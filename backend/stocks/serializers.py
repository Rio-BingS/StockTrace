from rest_framework import serializers
from .models import Stock, StockGroup, DailyQuote, MinuteBar


class StockSerializer(serializers.ModelSerializer):
    group = serializers.PrimaryKeyRelatedField(
        queryset=StockGroup.objects.all(), required=False, allow_null=True,
    )

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


class DashboardStockSerializer(serializers.Serializer):
    """Dashboard 聚合数据 - 当日所有关注股票摘要（含可选持仓成本）"""
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField(allow_blank=True)
    group_id = serializers.IntegerField(allow_null=True)
    group_name = serializers.CharField(allow_null=True)
    cost_price = serializers.DecimalField(max_digits=10, decimal_places=3, allow_null=True)
    quantity = serializers.IntegerField(allow_null=True)
    trade_date = serializers.DateField(allow_null=True)
    # 价格类 3 位小数，与 DailyQuote 一致（DAT-11）
    open_price = serializers.DecimalField(max_digits=10, decimal_places=3, allow_null=True)
    close_price = serializers.DecimalField(max_digits=10, decimal_places=3, allow_null=True)
    high_price = serializers.DecimalField(max_digits=10, decimal_places=3, allow_null=True)
    low_price = serializers.DecimalField(max_digits=10, decimal_places=3, allow_null=True)
    high_time = serializers.DateTimeField(allow_null=True)
    low_time = serializers.DateTimeField(allow_null=True)
    open_close_diff = serializers.DecimalField(max_digits=10, decimal_places=3, allow_null=True)
    open_close_pct = serializers.DecimalField(max_digits=8, decimal_places=4, allow_null=True)
    high_low_diff = serializers.DecimalField(max_digits=10, decimal_places=3, allow_null=True)
    high_low_pct = serializers.DecimalField(max_digits=8, decimal_places=4, allow_null=True)
    prev_close = serializers.DecimalField(max_digits=10, decimal_places=3, allow_null=True)
    change_diff = serializers.DecimalField(max_digits=10, decimal_places=3, allow_null=True)
    change_pct = serializers.DecimalField(max_digits=8, decimal_places=4, allow_null=True)
    volume = serializers.IntegerField(allow_null=True)
    turnover = serializers.DecimalField(max_digits=15, decimal_places=2, allow_null=True)
