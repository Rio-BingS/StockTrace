"""个股融资明细日表 StockMarginDaily：解析、落库、深市偿还额推导与补拉（det-v2）。"""

from datetime import date
from unittest.mock import patch

import pandas as pd
from django.test import TestCase

from .market import margin_stock
from .models import StockMarginDaily


def _sse_df(day, rows):
    return pd.DataFrame([
        {'信用交易日期': day, '标的证券代码': code, '标的证券简称': 'X', '融资余额': rz,
         '融资买入额': buy, '融资偿还额': repay, '融券余量': 0, '融券卖出量': 0, '融券偿还量': 0}
        for code, rz, buy, repay in rows
    ])


def _sz_df(rows):
    return pd.DataFrame([
        {'证券代码': code, '证券简称': 'Y', '融资买入额': buy, '融资余额': rz,
         '融券卖出量': 0, '融券余量': 0, '融券余额': 0, '融资融券余额': rz}
        for code, rz, buy in rows
    ])


class StockMarginRowsTests(TestCase):
    def test_sse_rows_keep_buy_and_repay_in_yuan(self):
        df = _sse_df('20261009', [('600000', 1.5e9, 3.2e7, 2.9e7), (600001, 2e8, 1e6, 2e6)])
        rows = margin_stock._raw_rows_sse(df, date(2026, 10, 9))
        self.assertEqual(rows['600000'], {'rz': 1500000000, 'rz_buy': 32000000, 'rz_repay': 29000000})
        # 代码被读成整数时补齐 6 位
        self.assertIn('600001', rows)

    def test_sse_rows_with_other_credit_date_are_rejected(self):
        # 接口回落到别的信用交易日时整表作废，不能把别日的数写成请求日
        df = _sse_df('20261008', [('600000', 1.5e9, 3.2e7, 2.9e7)])
        self.assertEqual(margin_stock._raw_rows_sse(df, date(2026, 10, 9)), {})

    def test_fetch_returns_none_on_upstream_error(self):
        with patch('akshare.stock_margin_detail_szse', side_effect=OSError('ssl')):
            self.assertIsNone(margin_stock.fetch_margin_day_rows('szse', date(2026, 10, 9)))


class SaveStockMarginDayTests(TestCase):
    def test_save_sse_day_and_idempotent_rerun(self):
        df = _sse_df('20261009', [('600000', 1.5e9, 3.2e7, 2.9e7)])
        with patch('akshare.stock_margin_detail_sse', return_value=df):
            self.assertEqual(margin_stock.save_stock_margin_day('sse', date(2026, 10, 9)), 1)
            self.assertEqual(margin_stock.save_stock_margin_day('sse', date(2026, 10, 9)), 1)
        row = StockMarginDaily.objects.get(code='600000')
        self.assertEqual((row.rz, row.rz_buy, row.rz_repay, row.rz_repay_derived),
                         (1500000000, 32000000, 29000000, False))
        self.assertEqual(StockMarginDaily.objects.count(), 1)

    def test_szse_repay_derived_from_previous_balance(self):
        with patch('akshare.stock_margin_detail_szse', return_value=_sz_df([('000001', 1000, 100)])):
            margin_stock.save_stock_margin_day('szse', date(2026, 10, 8))
        with patch('akshare.stock_margin_detail_szse',
                   return_value=_sz_df([('000001', 1050, 300), ('300750', 500, 20)])):
            margin_stock.save_stock_margin_day('szse', date(2026, 10, 9))
        first = StockMarginDaily.objects.get(code='000001', trade_date=date(2026, 10, 8))
        # 首日没有前一日余额：推不出，留空而不是补 0
        self.assertIsNone(first.rz_repay)
        self.assertFalse(first.rz_repay_derived)
        row = StockMarginDaily.objects.get(code='000001', trade_date=date(2026, 10, 9))
        self.assertEqual(row.rz_repay, 1000 + 300 - 1050)
        self.assertTrue(row.rz_repay_derived)
        new = StockMarginDaily.objects.get(code='300750')
        self.assertIsNone(new.rz_repay)

    def test_no_data_writes_nothing(self):
        with patch('akshare.stock_margin_detail_sse', return_value=pd.DataFrame()):
            self.assertEqual(margin_stock.save_stock_margin_day('sse', date(2026, 10, 9)), 0)
        self.assertFalse(StockMarginDaily.objects.exists())


class SyncRecentStockMarginTests(TestCase):
    def test_only_missing_exchange_days_are_fetched(self):
        days = [date(2026, 10, 8), date(2026, 10, 9)]
        StockMarginDaily.objects.create(code='600000', trade_date=days[0], exchange='sse', rz=1)
        StockMarginDaily.objects.create(code='000001', trade_date=days[0], exchange='szse', rz=1)
        calls = []

        def fake_save(kind, day, rows=None):
            calls.append((kind, day))
            return 5

        with patch('stocks.trading_calendar.recent_trading_days', return_value=days) as rtd, \
                patch.object(margin_stock, 'save_stock_margin_day', side_effect=fake_save):
            out = margin_stock.sync_recent_stock_margin(n_days=2, today=date(2026, 10, 12))
        # 只拉今天之前的信用交易日（T+1 披露）
        self.assertEqual(rtd.call_args.kwargs['end_date'], date(2026, 10, 11))
        self.assertEqual(calls, [('sse', days[1]), ('szse', days[1])])
        self.assertEqual(out, {'2026-10-09': {'sse': 5, 'szse': 5}})

    def test_calendar_unavailable_skips(self):
        with patch('stocks.trading_calendar.recent_trading_days', return_value=None):
            self.assertEqual(margin_stock.sync_recent_stock_margin(today=date(2026, 10, 12)), {})

    def test_days_present_requires_both_exchanges(self):
        d1, d2 = date(2026, 10, 8), date(2026, 10, 9)
        StockMarginDaily.objects.create(code='600000', trade_date=d1, exchange='sse', rz=1)
        StockMarginDaily.objects.create(code='000001', trade_date=d1, exchange='szse', rz=1)
        StockMarginDaily.objects.create(code='600000', trade_date=d2, exchange='sse', rz=1)
        self.assertEqual(margin_stock.stock_margin_days_present(d1, d2), {d1})
