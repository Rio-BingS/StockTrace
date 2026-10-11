"""AI 功能视图：Provider 配置、个股分析、条件选股、AI 翻译与点评"""
import logging

from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view
from rest_framework.response import Response

from ..models import AiCallLog, AiProvider, Stock
from ..ai_serializers import (
    AiProviderSerializer,
    ScreenerAiInputSerializer,
    ScreenerCommentInputSerializer,
    first_error_text,
)
from .llm import LlmClient, LlmError
from .prompts import (
    DISCLAIMER,
    build_analysis_messages,
    build_translate_messages,
    build_comment_messages,
)
from .screener import ConditionError, run_screener
from .throttle import (
    PROVIDER_TEST_PURPOSE,
    check_provider_test_throttle,
    check_throttle,
    log_call,
)

logger = logging.getLogger(__name__)


class AiProviderViewSet(viewsets.ModelViewSet):
    """AI 服务商配置 CRUD"""
    queryset = AiProvider.objects.all()
    serializer_class = AiProviderSerializer

    @action(detail=True, methods=['post'], url_path='test')
    def test_connection(self, request, pk=None):
        """用最小请求验证 Key 与连通性。

        只返回状态码与通用错误分类，不回显上游响应内容（防 SSRF 读取，SEC-01）。
        """
        provider = self.get_object()
        allowed, reason = check_provider_test_throttle()
        if not allowed:
            return Response({'detail': reason}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        # 先记流水再调用：频控按"预占"计，并发请求也会被计入
        call_log = AiCallLog.objects.create(
            purpose=PROVIDER_TEST_PURPOSE, provider=provider, success=False,
        )
        try:
            client = LlmClient(provider)
            _, usage = client.chat(
                [{'role': 'user', 'content': '回复"ok"两个字母即可'}],
                max_tokens=10,
                timeout=15,
                # 测试要快速给结论，不做重试
                max_retries=0,
            )
        except ValueError:
            return _test_failed('config', None)
        except LlmError as exc:
            if exc.kind == 'busy':
                # 并发已满：没有真正调用上游，撤销预占的流水
                call_log.delete()
                return _busy_response()
            if exc.kind not in _TEST_OK_KINDS:
                # 详情只进服务端日志（不含上游响应体），对外仅给分类
                logger.warning('AI 服务商 %s 连通性测试失败：kind=%s status=%s',
                               provider.pk, exc.kind, exc.status_code)
                return _test_failed(exc.kind, exc.status_code)
            # 空回复/被截断说明 Key 与地址都通了（max_tokens 很小，推理模型常见）
            usage = exc.usage
        call_log.success = True
        call_log.prompt_tokens = (usage or {}).get('prompt_tokens')
        call_log.completion_tokens = (usage or {}).get('completion_tokens')
        call_log.save(update_fields=['success', 'prompt_tokens', 'completion_tokens'])
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


def _llm_failed(exc, purpose, provider, stock=None):
    """AI 端点调用失败的统一出口：记流水、写日志，对外只给通用分类（SEC-07）。"""
    kind = getattr(exc, 'kind', None) or 'config'
    if kind == 'busy':
        return _busy_response()
    status_code = getattr(exc, 'status_code', None)
    log_call(purpose, provider=provider, stock=stock, success=False,
             usage=getattr(exc, 'usage', None))
    logger.warning('AI 调用失败 purpose=%s stock=%s kind=%s status=%s: %s',
                   purpose, stock.code if stock else '-', kind, status_code, exc)
    message = _TEST_ERROR_TEXT.get(kind, _TEST_ERROR_TEXT['upstream'])
    if status_code is not None and status_code != 200:
        message = f'{message}（HTTP {status_code}）'
    return Response(
        {'detail': message, 'error': kind, 'upstream_status': status_code},
        status=status.HTTP_502_BAD_GATEWAY,
    )


def _get_active_provider():
    # 显式按录入顺序取第一个启用的服务商，多个启用时行为可预期
    provider = AiProvider.objects.filter(is_enabled=True).order_by('pk').first()
    if provider is None:
        return None, ('未配置可用的 AI 服务商，请先在设置页添加')
    try:
        return LlmClient(provider), None
    except ValueError as exc:
        return None, str(exc)


@api_view(['POST'])
def stock_ai_analysis(request, pk):
    """个股 AI 分析：手动按需触发，同步返回"""
    stock = Stock.objects.filter(pk=pk, is_active=True).first()
    if stock is None:
        return Response({'detail': '股票不存在'}, status=status.HTTP_404_NOT_FOUND)

    allowed, reason = check_throttle('analysis', stock=stock)
    if not allowed:
        return Response({'detail': reason}, status=status.HTTP_429_TOO_MANY_REQUESTS)

    client, err = _get_active_provider()
    if err:
        return Response({'detail': err}, status=status.HTTP_400_BAD_REQUEST)

    try:
        text, usage = client.chat(build_analysis_messages(stock), max_tokens=1500)
    except (LlmError, ValueError) as exc:
        return _llm_failed(exc, 'analysis', client.provider, stock=stock)

    log_call('analysis', provider=client.provider, stock=stock, usage=usage)
    return Response({'analysis': text, 'disclaimer': DISCLAIMER})


@api_view(['POST'])
def screener(request):
    """结构化条件选股（可复现）"""
    try:
        rows = run_screener(request.data)
    except ConditionError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response({'count': len(rows), 'results': rows, 'disclaimer': None})


@api_view(['POST'])
def screener_ai(request):
    """自然语言 → LLM 翻译成结构化条件 → 复用引擎执行"""
    ser = ScreenerAiInputSerializer(data=request.data)
    if not ser.is_valid():
        return _invalid_input(ser.errors)
    query = ser.validated_data['query']

    allowed, reason = check_throttle('screener_translate')
    if not allowed:
        return Response({'detail': reason}, status=status.HTTP_429_TOO_MANY_REQUESTS)

    client, err = _get_active_provider()
    if err:
        return Response({'detail': err}, status=status.HTTP_400_BAD_REQUEST)

    try:
        spec, usage = client.chat_json(build_translate_messages(query))
    except (LlmError, ValueError) as exc:
        return _llm_failed(exc, 'screener_translate', client.provider)

    log_call('screener_translate', provider=client.provider, usage=usage)

    try:
        rows = run_screener(spec)
    except ConditionError as exc:
        # LLM 翻译出了引擎不认的条件：如实报错，不猜测修正
        return Response(
            {'detail': f'AI 生成的条件无法执行：{exc}', 'conditions': spec},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return Response({'conditions': spec, 'count': len(rows), 'results': rows})


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
        quote = stock.daily_quotes.order_by('-trade_date').first()
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

    allowed, reason = check_throttle('screener_comment')
    if not allowed:
        return Response({'detail': reason}, status=status.HTTP_429_TOO_MANY_REQUESTS)

    client, err = _get_active_provider()
    if err:
        return Response({'detail': err}, status=status.HTTP_400_BAD_REQUEST)

    try:
        text, usage = client.chat(build_comment_messages(rows, query), max_tokens=800)
    except (LlmError, ValueError) as exc:
        return _llm_failed(exc, 'screener_comment', client.provider)

    log_call('screener_comment', provider=client.provider, usage=usage)
    return Response({'comment': text, 'disclaimer': DISCLAIMER})
