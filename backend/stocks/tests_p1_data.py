"""P1 数据正确性与模型修复回归测试"""

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

import pandas as pd
from django.test import TestCase
from django.utils import timezone

from . import services
from .models import DailyQuote, MinuteBar, Stock
from .serializers import DailyQuoteSerializer, DashboardStockSerializer


def _aware(s):
    return timezone.make_aware(datetime.strptime(s, '%Y-%m-%d %H:%M:%S'))


class _NoSleep:
    def setUp(self):
        p = patch('stocks.services.time.sleep')
        p.start()
        self.addCleanup(p.stop)
        super().setUp()


class PricePrecisionTests(_NoSleep, TestCase):
    """DAT-11：ETF 三位小数报价存取不失真，成交额仍为 2 位。"""

    def setUp(self):
        super().setUp()
        self.stock = Stock.objects.create(code='510300', name='沪深300ETF')

    def test_daily_quote_keeps_three_decimals(self):
        df = pd.DataFrame([
            {'日期': '2026-09-01', '开盘': 1.230, '收盘': 1.234, '最高': 1.236, '最低': 1.229,
             '成交量': 100, '成交额': 123.456},
            {'日期': '2026-09-02', '开盘': 1.234, '收盘': 1.237, '最高': 1.238, '最低': 1.233,
             '成交量': 100, '成交额': 123.7},
        ])
        with patch('stocks.services._try_sources', return_value=(services.EastMoneySource(), df)):
            services.fetch_daily_data(self.stock, start_date='20260901', end_date='20260902')
        q = DailyQuote.objects.get(stock=self.stock, trade_date=date(2026, 9, 1))
        self.assertEqual(q.close_price, Decimal('1.234'))
        self.assertEqual(q.high_low_diff, Decimal('0.007'))
        self.assertEqual(q.turnover, Decimal('123.46'))
        q2 = DailyQuote.objects.get(stock=self.stock, trade_date=date(2026, 9, 2))
        self.assertEqual(q2.prev_close, Decimal('1.234'))
        self.assertEqual(q2.change_diff, Decimal('0.003'))
        self.assertEqual(DailyQuoteSerializer(q).data['close_price'], '1.234')

    def test_minute_bar_keeps_three_decimals(self):
        df = pd.DataFrame([{
            '时间': '2026-09-02 09:31:00', '开盘': 1.2341, '收盘': 1.2345,
            '最高': 1.236, '最低': 1.231, '成交量': 1000, '成交额': 1234.567,
        }])
        n = services._save_minute_bars(self.stock, df, date(2026, 9, 2), 'eastmoney')
        self.assertEqual(n, 1)
        bar = MinuteBar.objects.get(stock=self.stock)
        self.assertEqual(bar.open, Decimal('1.234'))
        self.assertEqual(bar.high, Decimal('1.236'))
        self.assertEqual(bar.turnover, Decimal('1234.57'))
        # 再写一遍不变的数据：与库内 3 位精度一致，不重复写
        with patch.object(MinuteBar.objects, 'bulk_create') as bc:
            services._save_minute_bars(self.stock, df, date(2026, 9, 2), 'eastmoney')
        bc.assert_not_called()

    def test_dashboard_serializer_three_decimals(self):
        data = DashboardStockSerializer({
            'id': 1, 'code': '510300', 'name': 'x', 'group_id': None, 'group_name': None,
            'cost_price': None, 'quantity': None, 'trade_date': None,
            'open_price': Decimal('1.234'), 'close_price': Decimal('1.235'),
            'high_price': None, 'low_price': None, 'high_time': None, 'low_time': None,
            'open_close_diff': Decimal('0.001'), 'open_close_pct': None,
            'high_low_diff': None, 'high_low_pct': None, 'prev_close': Decimal('1.230'),
            'change_diff': Decimal('0.005'), 'change_pct': None,
            'volume': None, 'turnover': None,
        }).data
        self.assertEqual(data['close_price'], '1.235')
        self.assertEqual(data['change_diff'], '0.005')
