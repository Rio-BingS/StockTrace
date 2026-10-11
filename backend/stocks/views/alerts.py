"""价格提醒：规则 CRUD 与触发记录。"""

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from ..common.envelope import list_response
from ..common.params import parse_bool
from ..models import Stock
from ..serializers import AlertSerializer


def _alert_row(alert):
    """兼容旧导入路径保留一版；新代码直接用 serializers.AlertSerializer（ARC-01）。"""
    return AlertSerializer(alert).data


def _alert_rule_error(stock_id, rule_type, threshold_raw):
    """校验创建提醒的入参；通过返回 None，否则返回错误消息。"""
    from decimal import Decimal, InvalidOperation

    from ..models import PriceAlert as _PA

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
    if not threshold.is_finite():
        return 'threshold 必须是数字'
    if rule_type in (_PA.PRICE_ABOVE, _PA.PRICE_BELOW) and threshold <= 0:
        return '价格阈值必须大于 0'
    if rule_type in (_PA.DAILY_PCT_ABOVE, _PA.DAILY_PCT_BELOW) and abs(threshold) == 0:
        return '涨跌幅阈值不能为 0'
    if abs(threshold) >= Decimal('1e8'):
        return 'threshold 超出范围'
    return None


def _normalize_threshold(rule_type, raw):
    """阈值统一为 4 位小数的 Decimal（模型 decimal_places=4）；涨跌幅规则存绝对值。

    旧实现把请求里的原值直接交给 ORM（ALG-14）：字符串/浮点各走各的转换，
    超精度的值在 SQLite 下原样落库；涨跌幅规则的负数阈值则让「涨幅达到 -2%」
    几乎每天触发。评估端已按绝对值判断，这里存绝对值让展示与判断一致。
    """
    from decimal import ROUND_HALF_UP, Decimal

    from ..models import PriceAlert as _PA

    value = Decimal(str(raw)).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
    if rule_type in (_PA.DAILY_PCT_ABOVE, _PA.DAILY_PCT_BELOW):
        value = abs(value)
    return value


@api_view(['GET', 'POST'])
def alert_list(request):
    """提醒规则列表 / 创建（同一股同一规则类型允许并存，由用户自己管理）"""
    from ..models import AlertEvent, PriceAlert

    if request.method == 'GET':
        alerts = PriceAlert.objects.select_related('stock').all()
        unread = AlertEvent.objects.filter(is_read=False).count()
        return Response({'unread_count': unread, 'items': AlertSerializer(alerts, many=True).data})

    stock_id = request.data.get('stock_id')
    rule_type = request.data.get('rule_type')
    error = _alert_rule_error(stock_id, rule_type, request.data.get('threshold'))
    if error:
        return Response({'detail': error}, status=status.HTTP_400_BAD_REQUEST)
    alert = PriceAlert.objects.create(
        stock_id=stock_id,
        rule_type=rule_type,
        threshold=_normalize_threshold(rule_type, request.data.get('threshold')),
        note=(request.data.get('note') or '').strip()[:100],
    )
    return Response(AlertSerializer(alert).data, status=status.HTTP_201_CREATED)


@api_view(['PATCH', 'DELETE'])
def alert_detail(request, pk):
    """启用/停用（PATCH {is_active}）或删除提醒规则"""
    from ..models import PriceAlert

    alert = PriceAlert.objects.filter(id=pk).select_related('stock').first()
    if alert is None:
        return Response({'detail': '提醒不存在'}, status=status.HTTP_404_NOT_FOUND)
    if request.method == 'DELETE':
        alert.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    if 'is_active' in request.data:
        # 按 DRF 布尔口径解析（ALG-14）：bool("false") 是 True，旧实现会把停用请求当启用
        from rest_framework import serializers as drf_serializers

        try:
            alert.is_active = drf_serializers.BooleanField().to_internal_value(request.data['is_active'])
        except drf_serializers.ValidationError:
            return Response({'detail': 'is_active 必须是布尔值'}, status=status.HTTP_400_BAD_REQUEST)
        alert.save(update_fields=['is_active'])
    if 'note' in request.data:
        alert.note = (request.data.get('note') or '').strip()[:100]
        alert.save(update_fields=['note'])
    return Response(AlertSerializer(alert).data)


ALERT_EVENT_LIMIT = 100


@api_view(['GET'])
def alert_event_list(request):
    """提醒触发记录（最近 100 条）；?unread=1 只看未读"""
    from ..models import AlertEvent

    events = AlertEvent.objects.select_related('alert', 'stock')
    # 旧口径只认小写 '1'/'true'，保持不变
    unread_only = parse_bool(request.query_params.get('unread'), truthy=('1', 'true'), ignore_case=False)
    if unread_only:
        events = events.filter(is_read=False)
    # 多取一条判断是否截断，外壳模式通过 meta.truncated 告知前端（ARC-04）
    rows = list(events[:ALERT_EVENT_LIMIT + 1])
    truncated = len(rows) > ALERT_EVENT_LIMIT
    return list_response(request, [{
        'id': event.id,
        'alert_id': event.alert_id,
        'stock_id': event.stock_id,
        'code': event.stock.code if event.stock else None,
        'name': event.stock.name if event.stock else None,
        'message': event.message,
        'trade_date': event.trade_date,
        'is_read': event.is_read,
        'created_at': event.created_at,
    } for event in rows[:ALERT_EVENT_LIMIT]], meta={
        'limit': ALERT_EVENT_LIMIT,
        'truncated': truncated,
        'unread_only': unread_only,
    })


@api_view(['POST'])
def alert_event_read(request):
    """标记已读：POST {ids: [..]} 或不传 ids 全部已读"""
    from ..models import AlertEvent

    ids = request.data.get('ids')
    qs = AlertEvent.objects.all()
    if ids is not None:
        if not isinstance(ids, list):
            return Response({'detail': 'ids 必须是数组'}, status=status.HTTP_400_BAD_REQUEST)
        qs = qs.filter(id__in=ids)
    updated = qs.filter(is_read=False).update(is_read=True)
    return Response({'marked': updated})
