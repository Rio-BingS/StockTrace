"""AI 功能视图：Provider 配置、个股分析、条件选股、AI 翻译与点评"""
import logging
import time

from django.conf import settings
from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view
from rest_framework.response import Response

from ..models import AiProvider, Stock
from ..ai_serializers import (
    AiProviderSerializer,
    ScreenerAiInputSerializer,
    ScreenerCommentInputSerializer,
    first_error_text,
)
from . import jobs as ai_jobs
from .llm import (
    LlmClient, LlmError, can_fallback, enabled_providers, hold_ai_slot, provider_ordering,
)
from .prompts import (
    DISCLAIMER,
    build_analysis_messages,
    build_translate_messages,
    build_comment_messages,
)
from .screener import ConditionError, run_screener_with_meta
from .throttle import (
    PROVIDER_TEST_PURPOSE,
    cancel_call,
    finish_call,
    reserve_call,
)

logger = logging.getLogger(__name__)


class AiProviderViewSet(viewsets.ModelViewSet):
    """AI 服务商配置 CRUD"""
    queryset = AiProvider.objects.all()
    serializer_class = AiProviderSerializer

    def get_queryset(self):
        # 列表顺序与调用时的选择/回退顺序一致：启用的在前，再按优先级（AIX-07）
        return AiProvider.objects.order_by('-is_enabled', *provider_ordering())

    @action(detail=True, methods=['post'], url_path='test')
    def test_connection(self, request, pk=None):
        """用最小请求验证 Key 与连通性。

        只返回状态码与通用错误分类，不回显上游响应内容（防 SSRF 读取，SEC-01）。
        """
        provider = self.get_object()
        # 频控检查与占位流水在同一事务内（AIX-06）：并发请求也会被计入
        call_log, reason = reserve_call(PROVIDER_TEST_PURPOSE, provider=provider)
        if call_log is None:
            return Response({'detail': reason}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        try:
            client = LlmClient(provider)
            _, usage = client.chat(
                [{'role': 'user', 'content': '回复"ok"两个字母即可'}],
                max_tokens=10,
                timeout=15,
                # 测试要快速给结论，不做重试
                max_retries=0,
            )
        except ValueError as exc:
            finish_call(call_log, False, error=LlmError(str(exc), kind='config'))
            return _test_failed('config', None)
        except LlmError as exc:
            if exc.kind == 'busy':
                # 并发已满：没有真正调用上游，撤销预占的流水
                cancel_call(call_log)
                return _busy_response()
            if exc.kind not in _TEST_OK_KINDS:
                finish_call(call_log, False, usage=exc.usage, error=exc)
                # 详情只进服务端日志（不含上游响应体），对外仅给分类
                logger.warning('AI 服务商 %s 连通性测试失败：kind=%s status=%s',
                               provider.pk, exc.kind, exc.status_code)
                return _test_failed(exc.kind, exc.status_code)
            # 空回复/被截断说明 Key 与地址都通了（max_tokens 很小，推理模型常见）
            usage = exc.usage
        finish_call(call_log, True, usage=usage)
        return Response({'status': 'ok'})


# test 端点视为"连接成功"的错误分类：上游已正常应答，只是内容为空或被截断
_TEST_OK_KINDS = ('empty', 'truncated')

# 对外的通用错误文案：所有 AI 端点只返回分类与状态码，不回显上游内容（SEC-07）
_TEST_ERROR_TEXT = {
    'config': '配置无效：接口地址不合规或 API Key 无法解密，请检查后重新录入',
    'network': '无法连接到服务商',
    'timeout': '连接服务商超时',
    'redirect': '服务商返回重定向，已拒绝跟随，请填写最终接口地址',
    'auth': '认证失败，请检查 API Key',
    'rate_limit': '服务商限流，请稍后再试',
    'bad_response': '服务商响应格式异常',
    'upstream': '服务商返回错误',
    'empty': '服务商返回了空内容',
    'truncated': 'AI 输出过长被截断，请稍后重试',
}


_BUSY_TEXT = '已有 AI 请求正在进行，请稍后再试'


def _busy_response():
    # 并发上限已满（AIX-02）：未调用上游，不记流水、不占每日额度
    return Response({'detail': _BUSY_TEXT, 'error': 'busy'},
                    status=status.HTTP_429_TOO_MANY_REQUESTS)


def _test_failed(kind, status_code):
    message = _TEST_ERROR_TEXT.get(kind, _TEST_ERROR_TEXT['upstream'])
    if status_code is not None:
        message = f'{message}（HTTP {status_code}）'
    return Response(
        {'status': 'failed', 'error': kind, 'upstream_status': status_code,
         'detail': message, 'message': message},
        status=status.HTTP_502_BAD_GATEWAY,
    )


def _llm_failed(exc, purpose, call_log, stock=None, provider=None, attempted=False):
    """AI 端点调用失败的统一出口：回填流水、写日志，对外只给通用分类（SEC-07）。"""
    kind = getattr(exc, 'kind', None) or 'config'
    if kind == 'busy' and not attempted:
        # 并发已满且没有调用过任何上游：撤销占位，不占额度
        cancel_call(call_log)
        return _busy_response()
    status_code = getattr(exc, 'status_code', None)
    finish_call(call_log, False, usage=getattr(exc, 'usage', None), error=exc,
                provider=provider)
    logger.warning('AI 调用失败 purpose=%s stock=%s kind=%s status=%s: %s',
                   purpose, stock.code if stock else '-', kind, status_code, exc)
    message = _TEST_ERROR_TEXT.get(kind, _TEST_ERROR_TEXT['upstream'])
    if status_code is not None and status_code != 200:
        message = f'{message}（HTTP {status_code}）'
    return Response(
        {'detail': message, 'error': kind, 'upstream_status': status_code},
        status=status.HTTP_502_BAD_GATEWAY,
    )


def _active_clients():
    """按选择顺序（与设置页列表一致）构造所有启用服务商的客户端。

    返回 (clients, 错误文案)。Key 解不开的服务商跳过；一个都用不了时返回错误。
    """
    providers = enabled_providers()
    if not providers:
        return [], '未配置可用的 AI 服务商，请先在设置页添加'
    clients, first_err = [], None
    for provider in providers:
        try:
            clients.append(LlmClient(provider))
        except ValueError as exc:
            logger.warning('AI 服务商 %s 不可用：%s', provider.pk, exc)
            first_err = first_err or str(exc)
    if not clients:
        return [], first_err
    return clients, None


def _prepare_llm(purpose, stock=None):
    """取可用服务商并预占额度。返回 ((clients, call_log), None) 或 (None, 错误 Response)。"""
    clients, err = _active_clients()
    if err:
        return None, Response({'detail': err}, status=status.HTTP_400_BAD_REQUEST)

    call_log, reason = reserve_call(purpose, provider=clients[0].provider, stock=stock)
    if call_log is None:
        return None, Response({'detail': reason}, status=status.HTTP_429_TOO_MANY_REQUESTS)
    return (clients, call_log), None


def _run_llm(purpose, invoke, stock=None):
    """预占额度后调用服务商（同步）。返回 (结果, None) 或 (None, 错误 Response)。"""
    prepared, failed = _prepare_llm(purpose, stock=stock)
    if failed:
        return None, failed
    return _execute_llm(purpose, invoke, prepared, stock=stock)


def _execute_llm(purpose, invoke, prepared, stock=None):
    """按顺序调用服务商，可回退的错误换下一个（AIX-07）。

    invoke(client) 返回 (结果, usage)。返回 (结果, None) 或 (None, 错误 Response)。
    整个请求只占一条流水与一次额度；回退只发生在上游明确未产出结果、
    且耗时未超过总时长三分之一时，避免超过 gunicorn/nginx 超时。
    """
    clients, call_log = prepared
    started = time.monotonic()
    budget = float(getattr(settings, 'STOCKTRACE_LLM_TOTAL_TIMEOUT', 90)) / 3
    attempted = False
    for idx, client in enumerate(clients):
        try:
            result, usage = invoke(client)
        except (LlmError, ValueError) as exc:
            if getattr(exc, 'kind', None) != 'busy':
                attempted = True
            has_next = idx + 1 < len(clients)
            if has_next and can_fallback(exc) and time.monotonic() - started < budget:
                logger.warning('AI 服务商 %s 调用失败（kind=%s status=%s），回退到下一个',
                               client.provider.pk, getattr(exc, 'kind', None),
                               getattr(exc, 'status_code', None))
                continue
            return None, _llm_failed(exc, purpose, call_log, stock=stock,
                                     provider=client.provider, attempted=attempted)
        except Exception as exc:
            # 意外异常也要回填占位，避免"进行中"流水一直挡住冷却
            finish_call(call_log, False, error=exc, provider=client.provider)
            raise
        finish_call(call_log, True, usage=usage, provider=client.provider)
        return result, None


@api_view(['POST'])
def stock_ai_analysis(request, pk):
    """个股 AI 分析：手动按需触发，同步返回"""
    stock = Stock.objects.filter(pk=pk, is_active=True).first()
    if stock is None:
        return Response({'detail': '股票不存在'}, status=status.HTTP_404_NOT_FOUND)

    messages = build_analysis_messages(stock)

    def invoke(client):
        return client.chat(messages, max_tokens=1500)

    if _wants_async(request):
        # 异步：同步完成校验与额度预占（429/400 立即返回），LLM 调用进后台队列
        prepared, failed = _prepare_llm('analysis', stock=stock)
        if failed:
            return failed

        def work():
            try:
                with hold_ai_slot(_job_slot_wait()):
                    text, failed = _execute_llm('analysis', invoke, prepared, stock=stock)
            except LlmError as exc:
                # 排队等名额超时：没有调用上游，撤销占位
                failed = _llm_failed(exc, 'analysis', prepared[1], stock=stock)
            if failed:
                return failed.status_code, failed.data
            return 200, {'analysis': text, 'disclaimer': DISCLAIMER}

        job_id = ai_jobs.submit('analysis', work, meta={'stock_id': stock.id})
        return Response(
            {'job_id': job_id, 'status': ai_jobs.STATUS_QUEUED,
             'poll_url': f'/api/ai/jobs/{job_id}/'},
            status=status.HTTP_202_ACCEPTED,
        )

    text, failed = _run_llm('analysis', invoke, stock=stock)
    if failed:
        return failed
    return Response({'analysis': text, 'disclaimer': DISCLAIMER})


def _wants_async(request):
    """?async=1 或 body {"async": true} 时走后台任务（默认仍同步，兼容旧前端）。"""
    raw = request.query_params.get('async')
    if raw is None and isinstance(request.data, dict):
        raw = request.data.get('async')
    return str(raw).lower() in ('1', 'true', 'yes')


def _job_slot_wait():
    # 后台任务排队等 AI 并发名额的上限：与单次调用总时长同量级，等不到就失败、不无限挂着
    return float(getattr(settings, 'STOCKTRACE_LLM_TOTAL_TIMEOUT', 90))


@api_view(['GET'])
def ai_job_detail(request, job_id):
    """查询 AI 后台任务：queued/running 时只有状态；done/failed 时带结果与原 HTTP 状态码。"""
    job = ai_jobs.get(job_id)
    if job is None:
        return Response({'detail': '任务不存在或已过期，请重新发起'},
                        status=status.HTTP_404_NOT_FOUND)
    body = {'job_id': job['job_id'], 'kind': job['kind'], 'status': job['status']}
    if 'queued_ahead' in job:
        body['queued_ahead'] = job['queued_ahead']
    if job['status'] in (ai_jobs.STATUS_DONE, ai_jobs.STATUS_FAILED):
        body['http_status'] = job.get('http_status')
        body['result'] = job.get('result')
    return Response(body)


@api_view(['POST'])
def screener(request):
    """结构化条件选股（可复现）"""
    try:
        rows, meta = run_screener_with_meta(request.data)
    except ConditionError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    # as_of/excluded：本次横截面的行情日与未参与的股票（ALG-12）
    return Response({'count': len(rows), 'results': rows, 'disclaimer': None, **meta})


@api_view(['POST'])
def screener_ai(request):
    """自然语言 → LLM 翻译成结构化条件 → 复用引擎执行"""
    ser = ScreenerAiInputSerializer(data=request.data)
    if not ser.is_valid():
        return _invalid_input(ser.errors)
    query = ser.validated_data['query']

    messages = build_translate_messages(query)
    spec, failed = _run_llm('screener_translate', lambda c: c.chat_json(messages))
    if failed:
        return failed

    try:
        rows, meta = run_screener_with_meta(spec)
    except ConditionError as exc:
        # LLM 翻译出了引擎不认的条件：如实报错，不猜测修正
        return Response(
            {'detail': f'AI 生成的条件无法执行：{exc}', 'conditions': spec},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return Response({'conditions': spec, 'count': len(rows), 'results': rows,
                     'disclaimer': DISCLAIMER, **meta})


def _invalid_input(errors):
    return Response({'detail': first_error_text(errors) or '参数不合法', 'errors': errors},
                    status=status.HTTP_400_BAD_REQUEST)


def _comment_rows(codes):
    """按代码从库里取名称与最新日线（AIX-01）：点评只基于服务端数据，不信客户端数字。"""
    codes = list(dict.fromkeys(codes))
    stocks = {s.code: s for s in Stock.objects.filter(is_active=True, code__in=codes)}
    rows = []
    for code in codes:
        stock = stocks.get(code)
        if stock is None:
            continue
        # 与选股执行口径一致，只用已收盘日线（DAT-13）
        quote = stock.daily_quotes.filter(is_final=True).order_by('-trade_date').first()
        rows.append({
            'code': stock.code,
            'name': stock.name,
            'trade_date': quote.trade_date if quote else None,
            'change_pct': quote.change_pct if quote else None,
            'close_price': quote.close_price if quote else None,
            'turnover': quote.turnover if quote else None,
        })
    return rows


@api_view(['POST'])
def screener_ai_comment(request):
    """对筛选结果做 AI 点评。入参：query（原始需求）+ results（screener 返回的 results）"""
    ser = ScreenerCommentInputSerializer(data=request.data)
    if not ser.is_valid():
        return _invalid_input(ser.errors)
    query = ser.validated_data['query']
    rows = _comment_rows([r['code'] for r in ser.validated_data['results']])
    if not rows:
        return Response({'detail': '结果中的股票都不在自选股池中'},
                        status=status.HTTP_400_BAD_REQUEST)

    messages = build_comment_messages(rows, query)
    text, failed = _run_llm(
        'screener_comment', lambda c: c.chat(messages, max_tokens=800))
    if failed:
        return failed
    return Response({'comment': text, 'disclaimer': DISCLAIMER})
