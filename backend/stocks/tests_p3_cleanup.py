"""P3 cleanup：全局日志清理（步骤 5d）与删除兼容别名的回归测试。"""

import logging
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pandas as pd
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from .models import DailyQuote, Stock


def _levels(cm):
    return [r.levelno for r in cm.records]


class DataLayerLoggingTests(TestCase):
    """5d 数据层：空结果降级 warning、单行解析收窄、批量边界记堆栈、吞异常补日志"""

    def setUp(self):
        self.stock = Stock.objects.create(code='600000', name='浦发银行')

    def test_daily_empty_result_is_warning_not_error(self):
        from stocks.ingest import fetch_daily_data

        with patch('stocks.ingest._try_sources', return_value=(None, None)):
            with self.assertLogs('stocks.ingest', level='DEBUG') as cm:
                self.assertEqual(fetch_daily_data(self.stock), 0)
        self.assertIn(logging.WARNING, _levels(cm))
        self.assertNotIn(logging.ERROR, _levels(cm))

    def test_minute_empty_result_is_warning_not_error(self):
        from stocks.ingest import fetch_minute_data

        with patch('stocks.ingest._try_sources', return_value=(None, None)):
            with self.assertLogs('stocks.ingest', level='DEBUG') as cm:
                self.assertIsNone(fetch_minute_data(self.stock, date(2026, 9, 1)))
        self.assertNotIn(logging.ERROR, _levels(cm))

    def test_zero_prev_close_row_still_skipped_after_narrowing(self):
        """昨收为 0 时 Decimal 除零：收窄后仍是逐行跳过（不抛出），与收窄前一致。

        跳过的行不推进昨收链，所以后一行同样以 0 为昨收被跳过（旧行为，不在本次修正）。
        """
        from stocks.datasources import SinaSource
        from stocks.ingest import fetch_daily_data

        d0 = timezone.localdate() - timedelta(days=3)
        DailyQuote.objects.create(
            stock=self.stock, trade_date=d0 - timedelta(days=1), open_price=Decimal('0'),
            close_price=Decimal('0'), high_price=Decimal('0'), low_price=Decimal('0'),
            open_close_diff=0, open_close_pct=0, high_low_diff=0, high_low_pct=0,
        )
        df = pd.DataFrame([
            {'日期': d0.isoformat(), '开盘': 10, '收盘': 10, '最高': 10, '最低': 10,
             '成交量': 1, '成交额': 1.0},
            {'日期': (d0 + timedelta(days=1)).isoformat(), '开盘': 10, '收盘': 11, '最高': 11,
             '最低': 10, '成交量': 1, '成交额': 1.0},
        ])
        with patch('stocks.ingest._try_sources', return_value=(SinaSource(), df)):
            with patch('stocks.ingest._adjust_basis_changed', return_value=[]):
                with self.assertLogs('stocks.ingest', level='WARNING') as cm:
                    n = fetch_daily_data(self.stock)
        self.assertEqual(n, 0)
        self.assertEqual(sum('解析 600000 日K行数据失败' in m for m in cm.output), 2)

    def test_batch_failure_logs_traceback(self):
        from stocks.jobs import fetch_all_active_stocks

        with patch('stocks.jobs.fetch_stock_all_data', side_effect=RuntimeError('boom')), \
                patch('stocks.jobs.time.sleep'):
            with self.assertLogs('stocks.jobs', level='ERROR') as cm:
                res = fetch_all_active_stocks()
        self.assertEqual(res[0]['status'], 'error')
        self.assertTrue(cm.output[0].startswith('ERROR:stocks.jobs:拉取 600000 数据失败: boom'))
        self.assertIsNotNone(cm.records[0].exc_info)


class DataSourceLoggingTests(SimpleTestCase):
    def test_sina_name_failure_is_logged(self):
        from stocks.datasources import SinaSource

        with patch('stocks.datasources.sina.ak.stock_zh_a_spot', side_effect=ValueError('bad')):
            with self.assertLogs('stocks.datasources.sina', level='WARNING') as cm:
                self.assertIsNone(SinaSource().fetch_stock_name('600000'))
        self.assertIn('600000', cm.output[0])

    def test_stock_list_failure_falls_back_with_traceback(self):
        from stocks import search

        with patch.dict(search._stock_list_cache, {'df': None, 'expires': 0}), \
                patch('stocks.search.ak.stock_info_a_code_name', side_effect=OSError('net')):
            with self.assertLogs('stocks.search', level='WARNING') as cm:
                df = search._get_stock_list()
        self.assertTrue(df.empty)
        self.assertIsNotNone(cm.records[0].exc_info)

    def test_calendar_unexpected_error_logged(self):
        from stocks import trading_calendar

        with patch('stocks.trading_calendar.is_trading_day', side_effect=RuntimeError('x')):
            with self.assertLogs('stocks.trading_calendar', level='WARNING'):
                self.assertIsNone(trading_calendar.expected_latest_date())
