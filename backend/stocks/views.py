from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view
from rest_framework.response import Response
from django.conf import settings
from django.db import IntegrityError, close_old_connections, transaction
from django.db.models import OuterRef, Subquery
from django.utils import timezone
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date, datetime, timedelta
import logging
import threading
import time
import uuid

from .models import Stock, DailyQuote, MinuteBar
from .serializers import (
    normalize_stock_code,
    StockSerializer, DailyQuoteSerializer,
    MinuteBarSerializer, DashboardStockSerializer,
    StockSearchSerializer,
)
from .services import search_stocks
from .tasks import start_fetch_one, start_fetch_all, get_fetch_status
from .market import (
    get_market_overview,
    get_market_trend,
    get_sector_rotation,
    get_national_team_etfs,
    get_national_team_flow,
    get_market_fund_flow_window,
    get_northbound_window,
    get_etf_share_radar,
    get_etf_detail,
    fetch_stock_margin,
    get_institution_holdings,
)
from .market.periods import FULL_PRESETS, resolve_period

logger = logging.getLogger(__name__)


def _initial_fetch(stock_id):
    """新增关注后的首拉：忙时排队而不是静默丢弃（REL-04），结果回给前端。"""
    started, st = start_fetch_one(stock_id, light=True, queue_if_busy=True)
    if started and not st.get('queued'):
        return {'status': 'started'}
    return {'status': 'queued', 'message': '已有拉取任务在执行，已排队，完成后自动补拉'}


class StockViewSet(viewsets.ModelViewSet):
    """关注股票 CRUD"""
    queryset = Stock.objects.filter(is_active=True)
    serializer_class = StockSerializer

    def create(self, request, *args, **kwargs):
        """添加关注：如果股票已存在但被软删除，则恢复；创建/恢复后后台拉行情"""
        code = normalize_stock_code(request.data.get('code', ''))
        existing = Stock.objects.filter(code=code).first() if code else None
        if existing and not existing.is_active:
            # 恢复分支也走序列化器校验（SEC-08）：name/group/持仓字段与新建同一套规则
            serializer = self.get_serializer(existing, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            name = serializer.validated_data.get('name', '') or ''
            if not name.strip():
                name = code
            serializer.save(is_active=True, name=name)
            data = dict(serializer.data)
            data['fetch'] = _initial_fetch(existing.id)
            return Response(data, status=status.HTTP_201_CREATED)
        try:
            with transaction.atomic():
                response = super().create(request, *args, **kwargs)
        except IntegrityError:
            # 先查后建竞态：并发请求已建同代码股票（PERF-06）
            return Response({'detail': f'股票「{code}」已存在'}, status=status.HTTP_409_CONFLICT)
        if response.status_code == status.HTTP_201_CREATED:
            stock_id = response.data.get('id')
            if stock_id:
                response.data['fetch'] = _initial_fetch(stock_id)
        return response

    def perform_create(self, serializer):
        """添加关注时，名称为空则用代码暂代（后续由数据拉取补全）"""
        code = serializer.validated_data['code']
        name = serializer.validated_data.get('name', '') or ''
        if not name.strip():
            name = code
        serializer.save(name=name)

    def perform_destroy(self, instance):
        """取消关注（软删除）"""
        instance.is_active = False
        instance.save()

    @action(detail=True, methods=['post'], url_path='fetch')
    def fetch_data(self, request, pk=None):
        """
        手动触发单只股票数据拉取（后台线程，立即返回）。
        默认 light（增量日线 + 近2日分钟）；?full=1 或 body full=true 为更全分钟。
        """
        stock = self.get_object()
        full = str(request.query_params.get('full', '')).lower() in ('1', 'true', 'yes')
        if request.data and str(request.data.get('full', '')).lower() in ('1', 'true', 'yes'):
            full = True
        started, st = start_fetch_one(stock.id, light=not full)
        if not started:
            return Response(
                {
                    'status': 'busy',
                    'message': '已有拉取任务在执行',
                    'fetch': st,
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response({
            'status': 'started',
            'code': stock.code,
            'mode': 'full' if full else 'light',
            'fetch': st,
        })

    @action(detail=False, methods=['get'], url_path='search')
    def search(self, request):
        """搜索股票（支持代码或名称模糊匹配）"""
        keyword = request.query_params.get('q', '')
        results = search_stocks(keyword)
        serializer = StockSearchSerializer(results, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['post'], url_path='fetch-all')
    def fetch_all(self, request):
        """
        手动触发所有关注股票数据拉取（后台线程，立即返回）。
        默认 light；?full=1 才拉更全分钟（仍按股串行）。
        """
        full = str(request.query_params.get('full', '')).lower() in ('1', 'true', 'yes')
        if request.data and str(request.data.get('full', '')).lower() in ('1', 'true', 'yes'):
            full = True
        started, st = start_fetch_all(light=not full)
        if not started:
            return Response(
                {
                    'status': 'busy',
                    'message': '已有拉取任务在执行',
                    'fetch': st,
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response({'status': 'started', 'mode': 'full' if full else 'light', 'fetch': st})

    @action(detail=False, methods=['get'], url_path='fetch-status')
    def fetch_status(self, request):
        """查询后台拉取任务状态"""
        return Response(get_fetch_status())

    @action(detail=True, methods=['get'], url_path='daily')
    def daily_quotes(self, request, pk=None):
        """获取某股票日K数据，支持 ?days=N（上限 3650）和 ?date=YYYY-MM-DD；缺省近 365 天"""
        stock = self.get_object()
        # select_related：序列化器 source='stock.code' 否则每行懒加载一次（PERF-05）
        queryset = DailyQuote.objects.filter(stock=stock).select_related('stock')

        # 按日期筛选
        if date_str := request.query_params.get('date'):
            try:
                date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response(
                    {'detail': 'date 格式应为 YYYY-MM-DD'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            queryset = queryset.filter(trade_date=date_obj)
        elif days_raw := request.query_params.get('days'):
            try:
                days = int(days_raw)
            except (TypeError, ValueError):
                return Response(
                    {'detail': 'days 必须是正整数'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if days < 1:
                return Response(
                    {'detail': 'days 必须是正整数'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            start_date = timezone.localdate() - timedelta(days=min(days, 3650))
            queryset = queryset.filter(trade_date__gte=start_date)
        else:
            # 无参数默认近 365 天，不再整表倒出（PERF-05）
            queryset = queryset.filter(trade_date__gte=timezone.localdate() - timedelta(days=365))

        queryset = queryset.order_by('-trade_date')
        serializer = DailyQuoteSerializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'], url_path='daily/latest')
    def daily_latest(self, request, pk=None):
        """获取某股票最新一天行情（含高低点时间）"""
        stock = self.get_object()
        try:
            latest = DailyQuote.objects.filter(stock=stock).latest('trade_date')
            serializer = DailyQuoteSerializer(latest)
            return Response(serializer.data)
        except DailyQuote.DoesNotExist:
            return Response(
                {'detail': '暂无数据'},
                status=status.HTTP_404_NOT_FOUND
            )

    @action(detail=True, methods=['get'], url_path='minutes')
    def minute_bars(self, request, pk=None):
        """获取分钟K线数据，支持 ?date=YYYY-MM-DD；缺省返回最近一个交易日（防整表倒出）"""
        stock = self.get_object()
        if date_str := request.query_params.get('date'):
            try:
                date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response(
                    {'detail': 'date 格式应为 YYYY-MM-DD'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            latest = MinuteBar.objects.filter(stock=stock).order_by('-datetime').first()
            if latest is None:
                return Response([])
            date_obj = timezone.localtime(latest.datetime).date()

        start = timezone.make_aware(datetime.combine(date_obj, datetime.min.time()))
        end = start + timedelta(days=1)
        queryset = MinuteBar.objects.filter(stock=stock, datetime__range=(start, end))

        queryset = queryset.order_by('datetime')
        serializer = MinuteBarSerializer(queryset, many=True)
        return Response(serializer.data)


def _quote_row(stock, quote):
    base = {
        'id': stock.id,
        'code': stock.code,
        'name': stock.name,
        'group_id': stock.group_id,
        'group_name': stock.group.name if stock.group_id else None,
        'cost_price': stock.cost_price,
        'quantity': stock.quantity,
    }
    if quote is None:
        base.update({
            'trade_date': None,
            'open_price': None,
            'close_price': None,
            'high_price': None,
            'low_price': None,
            'high_time': None,
            'low_time': None,
            'open_close_diff': None,
            'open_close_pct': None,
            'high_low_diff': None,
            'high_low_pct': None,
            'prev_close': None,
            'change_diff': None,
            'change_pct': None,
            'volume': None,
            'turnover': None,
        })
        return base
    base.update({
        'trade_date': quote.trade_date,
        'open_price': quote.open_price,
        'close_price': quote.close_price,
        'high_price': quote.high_price,
        'low_price': quote.low_price,
        'high_time': quote.high_time,
        'low_time': quote.low_time,
        'open_close_diff': quote.open_close_diff,
        'open_close_pct': quote.open_close_pct,
        'high_low_diff': quote.high_low_diff,
        'high_low_pct': quote.high_low_pct,
        'prev_close': quote.prev_close,
        'change_diff': quote.change_diff,
        'change_pct': quote.change_pct,
        'volume': quote.volume,
        'turnover': quote.turnover,
    })
    return base


@api_view(['GET'])
def dashboard(request):
    """Dashboard 聚合：自选 + 每只最新日行情（2 次查询，避免 N+1）"""
    # 每只最新一行日线用相关子查询取 id（走 stock+trade_date 唯一索引），
    # 不再每 60s 轮询都遍历全部历史日线（PERF-05）
    latest = DailyQuote.objects.filter(stock_id=OuterRef('pk')).order_by('-trade_date').values('id')[:1]
    stocks = list(
        Stock.objects.filter(is_active=True).select_related('group')
        .annotate(latest_quote_id=Subquery(latest)).order_by('code')
    )
    if not stocks:
        return Response([])

    quotes = DailyQuote.objects.in_bulk([s.latest_quote_id for s in stocks if s.latest_quote_id])
    latest_map = {s.id: quotes.get(s.latest_quote_id) for s in stocks}

    data = [_quote_row(s, latest_map.get(s.id)) for s in stocks]
    serializer = DashboardStockSerializer(data, many=True)
    return Response(serializer.data)


# ------------------------------------------------------------
# 同步拉上游的市场接口：总时限 + 熔断（REL-05）
# 请求线程把 loader 交给有界线程池，最多等 STOCKTRACE_REQUEST_DEADLINE 秒（默认 100，
# 小于 nginx proxy_read_timeout 120s），超时返回 504 而不是让 nginx 先断开、worker 继续空等。
# 同一接口连续 3 次超时即熔断 60s，期间直接 503，避免挂死的上游把线程池占满。
# ------------------------------------------------------------
_UPSTREAM_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix='upstream')
_BREAKER_THRESHOLD = 3
_BREAKER_OPEN_SECONDS = 60
_breaker_lock = threading.Lock()
_breaker = {}  # label -> {'timeouts': 连续超时次数, 'open_until': monotonic 秒}


class UpstreamTimeout(Exception):
    pass


class UpstreamCircuitOpen(Exception):
    pass


def _request_deadline():
    return float(getattr(settings, 'STOCKTRACE_REQUEST_DEADLINE', 100))


def _run_loader(loader):
    try:
        return loader()
    finally:
        close_old_connections()  # 池线程长驻：用完归还 DB 连接


def _call_with_deadline(loader, label):
    """在线程池里执行 loader，带总时限与按 label 熔断。超时抛 UpstreamTimeout，熔断中抛 UpstreamCircuitOpen。"""
    now = time.monotonic()
    with _breaker_lock:
        st = _breaker.get(label)
        if st and st['open_until'] > now:
            raise UpstreamCircuitOpen(label)
    future = _UPSTREAM_POOL.submit(_run_loader, loader)
    try:
        result = future.result(timeout=_request_deadline())
    except FutureTimeout:
        future.cancel()  # 还在排队则不再执行；已在跑的线程无法强停，由上游 socket 超时兜底
        with _breaker_lock:
            st = _breaker.setdefault(label, {'timeouts': 0, 'open_until': 0.0})
            st['timeouts'] += 1
            if st['timeouts'] >= _BREAKER_THRESHOLD:
                st['open_until'] = time.monotonic() + _BREAKER_OPEN_SECONDS
                st['timeouts'] = 0
                logger.warning(f'{label} 连续 {_BREAKER_THRESHOLD} 次超时，熔断 {_BREAKER_OPEN_SECONDS}s')
        raise UpstreamTimeout(label)
    with _breaker_lock:
        _breaker.pop(label, None)
    return result


def _deadline_error_response(exc, label):
    if isinstance(exc, UpstreamCircuitOpen):
        return Response(
            {'detail': f'{label}上游连续超时，暂停访问 {_BREAKER_OPEN_SECONDS} 秒后再试'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    logger.warning(f'获取{label}超过 {_request_deadline():.0f}s 总时限')
    return Response({'detail': f'获取{label}超时，请稍后重试'}, status=status.HTTP_504_GATEWAY_TIMEOUT)


def _market_response(loader, label):
    try:
        return Response(_call_with_deadline(loader, label))
    except (UpstreamTimeout, UpstreamCircuitOpen) as e:
        return _deadline_error_response(e, label)
    except Exception:
        return _upstream_failed(label)


def _upstream_failed(label):
    """上游/内部异常的统一出口：对外只给通用文案与 error_id，详情进日志（SEC-07）。

    异常原文可能含上游响应片段、内网地址或文件路径，不回显给客户端；
    error_id 同时写进日志，排障时按它在日志里定位堆栈。
    """
    error_id = uuid.uuid4().hex[:12]
    logger.exception(f'获取{label}失败 error_id={error_id}')
    return Response(
        {'detail': f'获取{label}失败，请稍后重试（错误编号 {error_id}）',
         'error_id': error_id},
        status=status.HTTP_502_BAD_GATEWAY,
    )


@api_view(['GET'])
def market_overview(request):
    """大盘首页：主要指数 + 资金/情绪摘要 + 二级模块入口"""
    return _market_response(get_market_overview, '大盘数据')


@api_view(['GET'])
def market_trend(request):
    """全市场走势：多指数归一化对比。

    区间三选一：?period=1w|1m|3m|6m|1y|ytd（默认 3m）、
    ?days=30|60|120|250（兼容旧参数）、?start=&end=（自定义，YYYY-MM-DD）。
    """
    def load():
        start = request.query_params.get('start')
        end = request.query_params.get('end')
        days_raw = request.query_params.get('days')
        if start or end:
            try:
                start_d = datetime.strptime(start, '%Y-%m-%d').date() if start else None
                end_d = datetime.strptime(end, '%Y-%m-%d').date() if end else timezone.localdate()
            except (TypeError, ValueError):
                raise ValueError('start/end 必须是 YYYY-MM-DD')
            start_d = start_d or end_d - timedelta(days=90)
            if start_d > end_d:
                raise ValueError('start 不能晚于 end')
            if (end_d - start_d).days > 1100:
                raise ValueError('自定义区间最长约 3 年')
            return get_market_trend(start=start_d.isoformat(), end=end_d.isoformat())
        if days_raw:
            days = _positive_int(request, 'days', 120)
            if days not in (30, 60, 120, 250):
                raise ValueError('days 仅支持 30、60、120、250')
            return get_market_trend(days=days)
        period = request.query_params.get('period', '3m')
        period_days = {'1w': 5, '1m': 22, '3m': 66, '6m': 130, '1y': 260}
        if period == 'ytd':
            today = timezone.localdate()
            days = max(5, int((today - date(today.year, 1, 1)).days * 5 / 7))
        elif period in period_days:
            days = period_days[period]
        else:
            raise ValueError('period 必须是 1w、1m、3m、6m、1y 或 ytd')
        return get_market_trend(days=min(days, 300))
    return _market_bad_request(load, '全市场走势')


def _positive_int(request, key, default, maximum=None):
    raw = request.query_params.get(key)
    if raw in (None, ''):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f'{key} 必须是正整数')
    if value < 1:
        raise ValueError(f'{key} 必须是正整数')
    return min(value, maximum) if maximum else value


def _optional_float(request, key):
    raw = request.query_params.get(key)
    if raw in (None, ''):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f'{key} 必须是数字')
    if value < 0:
        raise ValueError(f'{key} 不能小于 0')
    return value


def _market_bad_request(loader, label='市场数据'):
    try:
        return Response(_call_with_deadline(loader, label))
    except (UpstreamTimeout, UpstreamCircuitOpen) as e:
        return _deadline_error_response(e, label)
    except ValueError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception:
        return _upstream_failed(label)


@api_view(['GET'])
def market_sectors(request):
    """板块资金轮动：行业或概念的当日完整排行。"""
    def load():
        return get_sector_rotation(
            board=request.query_params.get('board', 'industry'),
            period=request.query_params.get('period', 'day'),
            q=(request.query_params.get('q') or '').strip(),
            sort=request.query_params.get('sort', 'net'),
            order=request.query_params.get('order', 'desc'),
            page=_positive_int(request, 'page', 1),
            page_size=_positive_int(request, 'page_size', 50, maximum=100),
        )
    return _market_bad_request(load, '板块资金轮动')


@api_view(['GET'])
def market_national_etf(request):
    """国家队相关 ETF 观察名单（非官方持仓）。"""
    return _market_response(get_national_team_etfs, '国家队相关ETF观察')


@api_view(['GET'])
def market_national_etf_flow(request):
    """国家队 ETF 区间资金流向：?period=1d|3d|5d|1w|1m|3m|6m|ytd 或 ?start=&end= 自定义。"""
    def load():
        return get_national_team_flow(
            period=request.query_params.get('period', '3m'),
            start=request.query_params.get('start'),
            end=request.query_params.get('end'),
        )
    return _market_bad_request(load, '国家队ETF资金流向')


@api_view(['GET'])
def market_etf_radar(request):
    """ETF 快照雷达：范围、筛选、排序与分页。"""
    def load():
        return get_etf_share_radar(
            scope=request.query_params.get('scope', 'equity_broad'),
            rank=request.query_params.get('rank', 'share'),
            sort=request.query_params.get('sort') or None,
            order=request.query_params.get('order') or None,
            q=(request.query_params.get('q') or '').strip(),
            min_turnover=_optional_float(request, 'min_turnover'),
            page=_positive_int(request, 'page', 1),
            page_size=_positive_int(request, 'page_size', 50, maximum=100),
        )
    return _market_bad_request(load, 'ETF雷达')


@api_view(['GET'])
def market_etf_detail(request, code):
    """单 ETF 当前快照与按需价格历史。"""
    norm = normalize_stock_code(code)
    if norm is None:
        return Response({'detail': 'ETF 代码须为 6 位数字'}, status=status.HTTP_400_BAD_REQUEST)
    return _market_bad_request(lambda: get_etf_detail(
        code=norm,
        range_name=request.query_params.get('range', '3m'),
        start_date=request.query_params.get('start_date') or None,
        end_date=request.query_params.get('end_date') or None,
    ), 'ETF详情')


@api_view(['GET'])
def market_stock_margin(request, code):
    """个股两融余额（交易所官方披露，T+1）。"""
    # 路由是 <str:code>，先校验格式再进入上游请求与缓存键（SEC-08）
    norm = normalize_stock_code(code)
    if norm is None:
        return Response({'detail': '股票代码须为 6 位数字'}, status=status.HTTP_400_BAD_REQUEST)
    return _market_bad_request(lambda: fetch_stock_margin(norm), '个股两融')


@api_view(['GET'])
def market_fund_flow_window(request):
    """大盘主力资金流区间聚合：?period= 档位 或 ?start=&end= 自定义。"""
    def load():
        window = resolve_period(request.query_params.get, FULL_PRESETS, default='1m')
        return get_market_fund_flow_window(window)
    return _market_bad_request(load, '大盘资金流')


@api_view(['GET'])
def market_northbound_window(request):
    """北向资金净买额区间聚合：?period= 档位 或 ?start=&end= 自定义。"""
    def load():
        window = resolve_period(request.query_params.get, FULL_PRESETS, default='1m')
        return get_northbound_window(window)
    return _market_bad_request(load, '北向资金')


@api_view(['GET'])
def market_institutions(request):
    """机构持仓：按股汇总 / 按机构变动 / 北向序列；?code= 可选个股明细，
    ?quarter=2026Q1 可指定机构持股汇总的报告期（默认最近有数据的季度）。"""
    code = (request.query_params.get('code') or '').strip() or None
    quarter = (request.query_params.get('quarter') or '').strip() or None
    try:
        return Response(_call_with_deadline(
            lambda: get_institution_holdings(stock_code=code, quarter=quarter), '机构持仓'))
    except (UpstreamTimeout, UpstreamCircuitOpen) as e:
        return _deadline_error_response(e, '机构持仓')
    except ValueError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception:
        return _upstream_failed('机构持仓')


# ============================================================
# 价格提醒
# ============================================================

def _alert_row(alert):
    return {
        'id': alert.id,
        'stock_id': alert.stock_id,
        'code': alert.stock.code,
        'name': alert.stock.name,
        'rule_type': alert.rule_type,
        'rule_display': alert.get_rule_type_display(),
        'threshold': alert.threshold,
        'note': alert.note,
        'is_active': alert.is_active,
        'last_triggered_at': alert.last_triggered_at,
        'created_at': alert.created_at,
    }


def _alert_rule_error(stock_id, rule_type, threshold_raw):
    """校验创建提醒的入参；通过返回 None，否则返回错误消息。"""
    from decimal import Decimal, InvalidOperation

    from .models import PriceAlert as _PA

    if not stock_id:
        return '缺少 stock_id'
    if not Stock.objects.filter(id=stock_id, is_active=True).exists():
        return 'stock_id 不存在或未关注'
    if rule_type not in dict(_PA.RULE_CHOICES):
        return f'rule_type 不支持，可选：{", ".join(k for k, _ in _PA.RULE_CHOICES)}'
    try:
        threshold = Decimal(str(threshold_raw))
    except (TypeError, InvalidOperation):
        return 'threshold 必须是数字'
    if rule_type in (_PA.PRICE_ABOVE, _PA.PRICE_BELOW) and threshold <= 0:
        return '价格阈值必须大于 0'
    if rule_type in (_PA.DAILY_PCT_ABOVE, _PA.DAILY_PCT_BELOW) and abs(threshold) == 0:
        return '涨跌幅阈值不能为 0'
    return None


@api_view(['GET', 'POST'])
def alert_list(request):
    """提醒规则列表 / 创建（同一股同一规则类型允许并存，由用户自己管理）"""
    from .models import AlertEvent, PriceAlert

    if request.method == 'GET':
        alerts = PriceAlert.objects.select_related('stock').all()
        unread = AlertEvent.objects.filter(is_read=False).count()
        return Response({'unread_count': unread, 'items': [_alert_row(a) for a in alerts]})

    stock_id = request.data.get('stock_id')
    rule_type = request.data.get('rule_type')
    error = _alert_rule_error(stock_id, rule_type, request.data.get('threshold'))
    if error:
        return Response({'detail': error}, status=status.HTTP_400_BAD_REQUEST)
    alert = PriceAlert.objects.create(
        stock_id=stock_id,
        rule_type=rule_type,
        threshold=request.data.get('threshold'),
        note=(request.data.get('note') or '').strip()[:100],
    )
    return Response(_alert_row(alert), status=status.HTTP_201_CREATED)


@api_view(['PATCH', 'DELETE'])
def alert_detail(request, pk):
    """启用/停用（PATCH {is_active}）或删除提醒规则"""
    from .models import PriceAlert

    alert = PriceAlert.objects.filter(id=pk).select_related('stock').first()
    if alert is None:
        return Response({'detail': '提醒不存在'}, status=status.HTTP_404_NOT_FOUND)
    if request.method == 'DELETE':
        alert.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    if 'is_active' in request.data:
        alert.is_active = bool(request.data['is_active'])
        alert.save(update_fields=['is_active'])
    if 'note' in request.data:
        alert.note = (request.data.get('note') or '').strip()[:100]
        alert.save(update_fields=['note'])
    return Response(_alert_row(alert))


@api_view(['GET'])
def alert_event_list(request):
    """提醒触发记录（最近 100 条）；?unread=1 只看未读"""
    from .models import AlertEvent

    events = AlertEvent.objects.select_related('alert', 'stock')
    if request.query_params.get('unread') in ('1', 'true'):
        events = events.filter(is_read=False)
    return Response([{
        'id': event.id,
        'alert_id': event.alert_id,
        'stock_id': event.stock_id,
        'code': event.stock.code if event.stock else None,
        'name': event.stock.name if event.stock else None,
        'message': event.message,
        'trade_date': event.trade_date,
        'is_read': event.is_read,
        'created_at': event.created_at,
    } for event in events[:100]])


@api_view(['POST'])
def alert_event_read(request):
    """标记已读：POST {ids: [..]} 或不传 ids 全部已读"""
    from .models import AlertEvent

    ids = request.data.get('ids')
    qs = AlertEvent.objects.all()
    if ids is not None:
        if not isinstance(ids, list):
            return Response({'detail': 'ids 必须是数组'}, status=status.HTTP_400_BAD_REQUEST)
        qs = qs.filter(id__in=ids)
    updated = qs.filter(is_read=False).update(is_read=True)
    return Response({'marked': updated})


# ============================================================
# 条件选股预设
# ============================================================

@api_view(['GET', 'POST'])
def screener_preset_list(request):
    """预设列表 / 保存（spec 用与执行完全相同的校验规则，保存即可执行）"""
    from .ai.screener import ConditionError, _validate
    from .models import ScreenerPreset

    if request.method == 'GET':
        from .ai.strategies import BUILTIN_STRATEGIES

        presets = ScreenerPreset.objects.all()
        builtins = [{
            'id': None,
            'builtin': True,
            'name': s['name'],
            'desc': s.get('desc', ''),
            'spec': s['spec'],
            'created_at': None,
        } for s in BUILTIN_STRATEGIES]
        return Response(builtins + [{
            'id': preset.id,
            'builtin': False,
            'name': preset.name,
            'spec': preset.spec,
            'created_at': preset.created_at,
        } for preset in presets])

    name = (request.data.get('name') or '').strip()
    spec = request.data.get('spec')
    if not name or len(name) > 50:
        return Response({'detail': '预设名称必填且不超过 50 字'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        _validate(spec)
    except ConditionError as e:
        return Response({'detail': f'筛选条件不合法：{e}'}, status=status.HTTP_400_BAD_REQUEST)
    if ScreenerPreset.objects.filter(name=name).exists():
        return Response({'detail': f'预设「{name}」已存在'}, status=status.HTTP_409_CONFLICT)
    try:
        with transaction.atomic():
            preset = ScreenerPreset.objects.create(name=name, spec=spec)
    except IntegrityError:  # 并发同名创建（PERF-06）
        return Response({'detail': f'预设「{name}」已存在'}, status=status.HTTP_409_CONFLICT)
    return Response({'id': preset.id, 'name': preset.name, 'spec': preset.spec},
                    status=status.HTTP_201_CREATED)


@api_view(['DELETE'])
def screener_preset_detail(request, pk):
    from .models import ScreenerPreset

    preset = ScreenerPreset.objects.filter(id=pk).first()
    if preset is None:
        return Response({'detail': '预设不存在'}, status=status.HTTP_404_NOT_FOUND)
    preset.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# ============================================================
# 自选分组
# ============================================================

def _group_row(group):
    return {
        'id': group.id,
        'name': group.name,
        'order': group.order,
        'stock_count': group.stocks.filter(is_active=True).count(),
        'created_at': group.created_at,
    }


@api_view(['GET', 'POST'])
def stock_group_list(request):
    """分组列表（含各组活跃股票数）/ 新建"""
    from .models import StockGroup

    if request.method == 'GET':
        groups = StockGroup.objects.all()
        return Response([_group_row(g) for g in groups])

    name = (request.data.get('name') or '').strip()
    if not name or len(name) > 50:
        return Response({'detail': '分组名称必填且不超过 50 字'}, status=status.HTTP_400_BAD_REQUEST)
    if StockGroup.objects.filter(name=name).exists():
        return Response({'detail': f'分组「{name}」已存在'}, status=status.HTTP_409_CONFLICT)
    try:
        with transaction.atomic():
            group = StockGroup.objects.create(name=name)
    except IntegrityError:  # 并发同名创建（PERF-06）
        return Response({'detail': f'分组「{name}」已存在'}, status=status.HTTP_409_CONFLICT)
    return Response(_group_row(group), status=status.HTTP_201_CREATED)


@api_view(['PATCH', 'DELETE'])
def stock_group_detail(request, pk):
    """重命名/调序（PATCH {name?, order?}）；删除分组后组内股票变回未分组"""
    from .models import StockGroup

    group = StockGroup.objects.filter(id=pk).first()
    if group is None:
        return Response({'detail': '分组不存在'}, status=status.HTTP_404_NOT_FOUND)
    if request.method == 'DELETE':
        group.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    name = request.data.get('name')
    if name is not None:
        name = str(name).strip()
        if not name or len(name) > 50:
            return Response({'detail': '分组名称不能为空且不超过 50 字'}, status=status.HTTP_400_BAD_REQUEST)
        if StockGroup.objects.filter(name=name).exclude(id=group.id).exists():
            return Response({'detail': f'分组「{name}」已存在'}, status=status.HTTP_409_CONFLICT)
        group.name = name
    if 'order' in request.data:
        try:
            group.order = int(request.data['order'])
        except (TypeError, ValueError):
            return Response({'detail': 'order 必须是整数'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        with transaction.atomic():
            group.save()
    except IntegrityError:  # 并发重命名撞名（PERF-06）
        return Response({'detail': f'分组「{name}」已存在'}, status=status.HTTP_409_CONFLICT)
    return Response(_group_row(group))
