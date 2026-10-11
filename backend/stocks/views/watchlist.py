"""自选股票：StockViewSet（CRUD/拉取/日线/分钟）与 Dashboard 聚合。"""

from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.db.models import OuterRef, Subquery
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.response import Response

from ..common.params import parse_bool, parse_date, positive_int
from ..errors import BadRequest
from ..models import DailyQuote, MinuteBar, Stock
from ..serializers import (
    DailyQuoteSerializer,
    MinuteBarSerializer,
    QuoteRowSerializer,
    StockSearchSerializer,
    StockSerializer,
    normalize_stock_code,
)
from ..services import search_stocks
from ..tasks import get_fetch_status, start_fetch_all, start_fetch_one


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
        full = parse_bool(request.query_params.get('full'))
        if request.data and parse_bool(request.data.get('full')):
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
        full = parse_bool(request.query_params.get('full'))
        if request.data and parse_bool(request.data.get('full')):
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
                date_obj = parse_date(date_str, 'date', strict=True)
            except BadRequest as e:
                return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
            queryset = queryset.filter(trade_date=date_obj)
        elif request.query_params.get('days'):
            try:
                days = positive_int(request.query_params, 'days', 365, maximum=3650, strict=True)
            except BadRequest as e:
                return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
            start_date = timezone.localdate() - timedelta(days=days)
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
                date_obj = parse_date(date_str, 'date', strict=True)
            except BadRequest as e:
                return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
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
    """旧的 Dashboard 行字典（未经序列化的原始值）。

    dashboard 已改用 serializers.QuoteRowSerializer（ARC-01）；此函数仅为旧导入
    路径保留一版，下一版删除。
    """
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
    for s in stocks:
        s.latest_quote = quotes.get(s.latest_quote_id)

    return Response(QuoteRowSerializer(stocks, many=True).data)
