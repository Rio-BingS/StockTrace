"""P1 数据正确性与模型修复回归测试"""

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

import pandas as pd
from django.test import SimpleTestCase, TestCase
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


class ExchangeRouteTests(SimpleTestCase):
    """DAT-12：沪市 ETF（5 开头）与北交所前缀路由。"""

    def test_exchange(self):
        cases = {
            '600000': 'sh', '510300': 'sh', '588000': 'sh', '900901': 'sh',
            '000001': 'sz', '300750': 'sz', '159915': 'sz', '200002': 'sz',
            '920001': 'bj', '430047': 'bj', '830799': 'bj', '871981': 'bj',
        }
        for code, ex in cases.items():
            self.assertEqual(services._exchange(code), ex, code)
        self.assertEqual(services._to_shsz_prefix('510300'), 'sh510300')
        self.assertEqual(services._to_shsz_prefix('830799'), 'bj830799')

    def test_baostock_skips_bj(self):
        src = services.BaoStockSource()
        self.assertEqual(src._format_code('510300'), 'sh.510300')
        self.assertIsNone(src._format_code('830799'))
        with patch('baostock.login') as login:
            self.assertIsNone(src.fetch_daily('830799', '20260901', '20260902'))
            self.assertIsNone(src.fetch_minute('830799', '2026-09-02 09:30:00',
                                               '2026-09-02 15:00:00'))
            self.assertIsNone(src.fetch_stock_name('830799'))
        login.assert_not_called()


class BaoStockSessionTests(SimpleTestCase):
    """REL-06：多线程 BaoStock 调用串行化，不会互相登出。"""

    def test_sessions_do_not_interleave(self):
        import threading
        import time as _time

        state = {'logged_in': 0, 'max': 0, 'broken': 0}
        lock = threading.Lock()

        def login():
            with lock:
                state['logged_in'] += 1
                state['max'] = max(state['max'], state['logged_in'])

        def logout():
            with lock:
                state['logged_in'] -= 1

        class _RS:
            fields = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']

            def __init__(self):
                self.n = 0

            def next(self):
                _time.sleep(0.005)
                if state['logged_in'] != 1:
                    state['broken'] += 1
                self.n += 1
                return self.n <= 2

            def get_row_data(self):
                return ['2026-09-01', '1', '1', '1', '1', '100', '100']

        src = services.BaoStockSource()
        with patch('baostock.login', side_effect=login), \
                patch('baostock.logout', side_effect=logout), \
                patch('baostock.query_history_k_data_plus', side_effect=lambda *a, **k: _RS()):
            ts = [threading.Thread(target=src.fetch_daily, args=('600000', '20260901', '20260901'))
                  for _ in range(5)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(state['max'], 1)
        self.assertEqual(state['broken'], 0)
        self.assertEqual(state['logged_in'], 0)

    def test_logout_runs_and_lock_released_on_error(self):
        with patch('baostock.login'), patch('baostock.logout') as logout, \
                patch('baostock.query_stock_basic', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                services.BaoStockSource().fetch_stock_name('600000')
        logout.assert_called_once()
        self.assertFalse(services._bs_lock.locked())
