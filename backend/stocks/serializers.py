import re

from rest_framework import serializers
from .models import Stock, StockGroup, DailyQuote, MinuteBar

# A 股/ETF 代码：6 位数字（SEC-08）
STOCK_CODE_RE = re.compile(r'^\d{6}$')


def normalize_stock_code(value):
    """去首尾空白后校验 6 位数字代码；合法返回规范化代码，否则返回 None。"""
    code = str(value or '').strip()
    return code if STOCK_CODE_RE.match(code) else None


class StockSerializer(serializers.ModelSerializer):
    group = serializers.PrimaryKeyRelatedField(
        queryset=StockGroup.objects.all(), required=False, allow_null=True,
    )

    def validate_code(self, value):
        # 入库前统一校验格式，避免任意字符串进入行情拉取与缓存键（SEC-08）
        code = normalize_stock_code(value)
        if code is None:
            raise serializers.ValidationError('股票代码须为 6 位数字')
        return code

    class Meta:
        model = Stock
        fields = ['id', 'code', 'name', 'is_active', 'group', 'cost_price', 'quantity', 'created_at']
        read_only_fields = ['id', 'created_at']
        extra_kwargs = {
            'name': {'required': False, 'allow_blank': True},
            'cost_price': {'required': False, 'allow_null': True},
            'quantity': {'required': False, 'allow_null': True},
        }


class DailyQuoteSerializer(serializers.ModelSerializer):
    code = serializers.CharField(source='stock.code', read_only=True)
    name = serializers.CharField(source='stock.name', read_only=True)

    class Meta:
        model = DailyQuote
        fields = [
            'id', 'code', 'name', 'trade_date',
            'open_price', 'close_price', 'high_price', 'low_price',
            'high_time', 'low_time',
            'open_close_diff', 'open_close_pct',
            'high_low_diff', 'high_low_pct',
            'prev_close', 'change_diff', 'change_pct',
            'volume', 'turnover',
        ]


class MinuteBarSerializer(serializers.ModelSerializer):
    class Meta:
        model = MinuteBar
        fields = ['id', 'datetime', 'open', 'close', 'high', 'low', 'volume', 'turnover']


class StockSearchSerializer(serializers.Serializer):
    """股票搜索结果"""
    code = serializers.CharField()
    name = serializers.CharField()


# ------------------------------------------------------------
# 只读行序列化器（ARC-01）：取代 views 里手写的行字典（旧 _quote_row 等已删除）。
# 字段顺序与类型和旧字典逐一对应，tests_arc2_snapshots 锁定输出逐字节不变。
# ------------------------------------------------------------

def _quote(field, **kwargs):
    """Dashboard 行里来自最新日线的字段：无日线（latest_quote 为 None）时输出 null。"""
    return field(source=f'latest_quote.{kwargs.pop("name")}', allow_null=True, read_only=True, **kwargs)


class QuoteRowSerializer(serializers.Serializer):
    """Dashboard 行：自选股票 + 最新一天日线。

    instance 是带 `latest_quote` 属性（DailyQuote 或 None）的 Stock；
    输出与旧 DashboardStockSerializer + 行字典的组合完全一致（快照测试锁定）。
    """
    id = serializers.IntegerField(read_only=True)
    code = serializers.CharField(read_only=True)
    name = serializers.CharField(allow_blank=True, read_only=True)
    group_id = serializers.IntegerField(allow_null=True, read_only=True)
    group_name = serializers.CharField(source='group.name', allow_null=True, read_only=True)
    cost_price = serializers.DecimalField(max_digits=10, decimal_places=3, allow_null=True, read_only=True)
    quantity = serializers.IntegerField(allow_null=True, read_only=True)
    trade_date = _quote(serializers.DateField, name='trade_date')
    # 价格类 3 位小数，与 DailyQuote 一致（DAT-11）
    open_price = _quote(serializers.DecimalField, name='open_price', max_digits=10, decimal_places=3)
    close_price = _quote(serializers.DecimalField, name='close_price', max_digits=10, decimal_places=3)
    high_price = _quote(serializers.DecimalField, name='high_price', max_digits=10, decimal_places=3)
    low_price = _quote(serializers.DecimalField, name='low_price', max_digits=10, decimal_places=3)
    high_time = _quote(serializers.DateTimeField, name='high_time')
    low_time = _quote(serializers.DateTimeField, name='low_time')
    open_close_diff = _quote(serializers.DecimalField, name='open_close_diff', max_digits=10, decimal_places=3)
    open_close_pct = _quote(serializers.DecimalField, name='open_close_pct', max_digits=8, decimal_places=4)
    high_low_diff = _quote(serializers.DecimalField, name='high_low_diff', max_digits=10, decimal_places=3)
    high_low_pct = _quote(serializers.DecimalField, name='high_low_pct', max_digits=8, decimal_places=4)
    prev_close = _quote(serializers.DecimalField, name='prev_close', max_digits=10, decimal_places=3)
    change_diff = _quote(serializers.DecimalField, name='change_diff', max_digits=10, decimal_places=3)
    change_pct = _quote(serializers.DecimalField, name='change_pct', max_digits=8, decimal_places=4)
    volume = _quote(serializers.IntegerField, name='volume')
    turnover = _quote(serializers.DecimalField, name='turnover', max_digits=15, decimal_places=2)


class AlertSerializer(serializers.Serializer):
    """价格提醒规则行。

    全部用 ReadOnlyField 原样输出：旧实现把模型值直接交给 Response，
    threshold（Decimal）与时间由 JSON 编码器渲染（数字 / ISO 时间），
    换成 DecimalField/DateTimeField 会改变输出格式。
    """
    id = serializers.ReadOnlyField()
    stock_id = serializers.ReadOnlyField()
    code = serializers.ReadOnlyField(source='stock.code')
    name = serializers.ReadOnlyField(source='stock.name')
    rule_type = serializers.ReadOnlyField()
    rule_display = serializers.ReadOnlyField(source='get_rule_type_display')
    threshold = serializers.ReadOnlyField()
    note = serializers.ReadOnlyField()
    is_active = serializers.ReadOnlyField()
    last_triggered_at = serializers.ReadOnlyField()
    created_at = serializers.ReadOnlyField()


class StockGroupSerializer(serializers.Serializer):
    """自选分组行；stock_count 优先用列表查询 annotate 的 active_stock_count（PERF-08）。"""
    id = serializers.ReadOnlyField()
    name = serializers.ReadOnlyField()
    order = serializers.ReadOnlyField()
    stock_count = serializers.SerializerMethodField()
    created_at = serializers.ReadOnlyField()

    def get_stock_count(self, group):
        # 单个分组的新建/修改路径没有注解，退回单次计数
        count = getattr(group, 'active_stock_count', None)
        if count is None:
            count = group.stocks.filter(is_active=True).count()
        return count
