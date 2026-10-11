"""OPS-08 覆盖率补齐：services 数据源适配器、源切换、入库与定时入口。

只 mock 上游（akshare / baostock 函数）与 sleep，不 mock 被测函数本身。
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from . import services
from .models import DailyQuote, MinuteBar, Stock

D = date(2026, 9, 8)  # 周二


class _BsResult:
    """baostock ResultSet 替身：next()/get_row_data()/fields/error_code"""

    def __init__(self, rows, fields, error_code='0'):
        self._rows = list(rows)
        self.fields = fields
        self.error_code = error_code
        self.error_msg = ''

    def next(self):
        return bool(self._rows)

    def get_row_data(self):
        return self._rows.pop(0)


def _bs_ok():
    lg = MagicMock()
    lg.error_code = '0'
    return lg


class HelperTests(SimpleTestCase):
    def test_exchange_and_prefix(self):
        self.assertEqual(services._to_shsz_prefix('600000'), 'sh600000')
        self.assertEqual(services._to_shsz_prefix('1'), 'sz000001')
        self.assertEqual(services._to_shsz_prefix('920001'), 'bj920001')
        self.assertEqual(services._to_shsz_prefix('510300'), 'sh510300')

    def test_numeric_helpers_pass_through_empty(self):
        self.assertIsNone(services._to_numeric_cols(None))
        self.assertIsNone(services._volume_lots_to_shares(None))
        empty = pd.DataFrame()
        self.assertIs(services._volume_lots_to_shares(empty), empty)
        self.assertIsNone(services._filter_minute_range(None, 'a', 'b'))

    def test_volume_lots_to_shares_does_not_mutate_input(self):
        df = pd.DataFrame({'成交量': ['12'], '收盘': ['1.5']})
        out = services._volume_lots_to_shares(df)
        self.assertEqual(out['成交量'].iloc[0], 1200)
        self.assertEqual(df['成交量'].iloc[0], '12')

    def test_compute_high_low_times(self):
        t1, t2, t3 = (datetime(2026, 9, 8, 9, 30 + i) for i in range(3))
        self.assertIsNone(services.compute_high_low_times([]))
        self.assertIsNone(services.compute_high_low_times([(t1, None, None)]))
        # 并列取最早一根
        hl = services.compute_high_low_times([(t1, 10, 9), (t2, 11, 9), (t3, 11, 8)])
        self.assertEqual(hl, (t2, t3, 11, 8))

    def test_base_source_contract(self):
        src = services.DataSource()
        with self.assertRaises(NotImplementedError):
            src.fetch_daily('000001', '20260101', '20260102')
        with self.assertRaises(NotImplementedError):
            src.fetch_minute('000001', 'a', 'b')
        self.assertIsNone(src.fetch_stock_name('000001'))
        self.assertEqual(services.format_symbol('1'), '000001')


class SourceAdapterTests(SimpleTestCase):
    """DAT-03：各适配器返回前完成列名与单位归一（量=股、额=元）。"""

    def test_eastmoney_daily_and_minute_lots_to_shares(self):
        df = pd.DataFrame({'日期': ['2026-09-08'], '开盘': [1], '收盘': [2], '最高': [3],
                           '最低': [1], '成交量': [5], '成交额': [100.0]})
        with patch('akshare.stock_zh_a_hist', return_value=df.copy()) as hist:
            out = services.EastMoneySource().fetch_daily('000001', '20260901', '20260908')
        self.assertEqual(out['成交量'].iloc[0], 500)
        self.assertEqual(hist.call_args.kwargs['adjust'], 'qfq')
        mdf = pd.DataFrame({'时间': ['2026-09-08 09:31:00'], '成交量': [3]})
        with patch('akshare.stock_zh_a_hist_min_em', return_value=mdf):
            out = services.EastMoneySource().fetch_minute('000001', 'a', 'b')
        self.assertEqual(out['成交量'].iloc[0], 300)

    def test_eastmoney_name(self):
        spot = pd.DataFrame({'代码': ['000001', '600000'], '名称': ['平安银行', '浦发银行']})
        with patch('akshare.stock_zh_a_spot_em', return_value=spot):
            src = services.EastMoneySource()
            self.assertEqual(src.fetch_stock_name('600000'), '浦发银行')
            self.assertIsNone(src.fetch_stock_name('999999'))

    def test_sina_daily_renames_english_columns(self):
        df = pd.DataFrame({'date': ['2026-09-08'], 'open': [1.0], 'close': [2.0], 'high': [3.0],
                           'low': [0.5], 'volume': [1000], 'amount': [2000.0]})
        with patch('akshare.stock_zh_a_daily', return_value=df) as daily:
            out = services.SinaSource().fetch_daily('600000', '20260901', '20260908')
        self.assertEqual(daily.call_args.kwargs['symbol'], 'sh600000')
        self.assertEqual(list(out.columns), ['日期', '开盘', '收盘', '最高', '最低', '成交量', '成交额'])
        self.assertEqual(out['成交量'].iloc[0], 1000)  # 新浪原生为股，不换算

    def test_sina_minute_renames_and_casts(self):
        df = pd.DataFrame({'day': ['2026-09-08 09:31:00'], 'open': ['1.1'], 'close': ['1.2'],
                           'high': ['1.3'], 'low': ['1.0'], 'volume': ['500'], 'amount': ['600.5']})
        with patch('akshare.stock_zh_a_minute', return_value=df):
            out = services.SinaSource().fetch_minute('000001', 'a', 'b')
        self.assertIn('时间', out.columns)
        self.assertEqual(out['成交量'].iloc[0], 500)
        self.assertAlmostEqual(out['成交额'].iloc[0], 600.5)

    def test_sina_name_by_position_and_failure(self):
        spot = pd.DataFrame({'symbol': ['sh600000'], 'name': ['浦发银行']})
        with patch('akshare.stock_zh_a_spot', return_value=spot):
            self.assertEqual(services.SinaSource().fetch_stock_name('600000'), '浦发银行')
            self.assertIsNone(services.SinaSource().fetch_stock_name('000001'))
        with patch('akshare.stock_zh_a_spot', side_effect=RuntimeError('down')):
            self.assertIsNone(services.SinaSource().fetch_stock_name('600000'))

    def test_tencent_amount_is_volume_in_lots(self):
        df = pd.DataFrame({'date': ['2026-09-08'], 'open': [1.0], 'close': [2.0], 'high': [3.0],
                           'low': [0.5], 'amount': [7]})
        with patch('akshare.stock_zh_a_hist_tx', return_value=df):
            out = services.TencentSource().fetch_daily('000001', '20260901', '20260908')
        self.assertEqual(out['成交量'].iloc[0], 700)
        self.assertIsNone(out['成交额'].iloc[0])
        with patch('akshare.stock_zh_a_hist_tx', return_value=pd.DataFrame()):
            self.assertTrue(services.TencentSource().fetch_daily('000001', 'a', 'b').empty)
        self.assertIsNone(services.TencentSource().fetch_minute('000001', 'a', 'b'))
        self.assertIsNone(services.TencentSource().fetch_stock_name('000001'))

    def test_baostock_daily_minute_and_name(self):
        src = services.BaoStockSource()
        daily = _BsResult([['2026-09-08', '1', '3', '0.5', '2', '1000', '2000']],
                          ['date', 'open', 'high', 'low', 'close', 'volume', 'amount'])
        with patch('baostock.login', return_value=_bs_ok()), patch('baostock.logout'), \
                patch('baostock.query_history_k_data_plus', return_value=daily) as q:
            out = src.fetch_daily('600000', '20260901', '20260908')
        self.assertEqual(q.call_args.args[0], 'sh.600000')
        self.assertEqual(q.call_args.kwargs['start_date'], '2026-09-01')
        self.assertEqual(out['收盘'].iloc[0], 2.0)

        minute = _BsResult([['2026-09-08', '20260908093500000', '1', '2', '0.5', '1.5', '100', '150']],
                           ['date', 'time', 'open', 'high', 'low', 'close', 'volume', 'amount'])
        with patch('baostock.login', return_value=_bs_ok()), patch('baostock.logout'), \
                patch('baostock.query_history_k_data_plus', return_value=minute) as q:
            out = src.fetch_minute('000001', '2026-09-08 09:30:00', '2026-09-08 15:00:00')
        self.assertEqual(q.call_args.kwargs['frequency'], '5')
        self.assertEqual(out['时间'].iloc[0], pd.Timestamp('2026-09-08 09:35:00'))

        with patch('baostock.login', return_value=_bs_ok()), patch('baostock.logout'), \
                patch('baostock.query_stock_basic', return_value=_BsResult([['sh.600000', '浦发银行']], [])):
            self.assertEqual(src.fetch_stock_name('600000'), '浦发银行')

    def test_baostock_empty_and_beijing_unsupported(self):
        src = services.BaoStockSource()
        self.assertIsNone(src.fetch_daily('920001', 'a', 'b'))
        self.assertIsNone(src.fetch_minute('920001', 'a', 'b'))
        self.assertIsNone(src.fetch_stock_name('830799'))
        with patch('baostock.login', return_value=_bs_ok()), patch('baostock.logout'), \
                patch('baostock.query_history_k_data_plus', return_value=_BsResult([], [])):
            self.assertTrue(src.fetch_daily('600000', '20260901', '20260908').empty)
            self.assertTrue(src.fetch_minute('600000', '2026-09-08', '2026-09-08').empty)
        with patch('baostock.login', return_value=_bs_ok()), patch('baostock.logout'), \
                patch('baostock.query_stock_basic', return_value=_BsResult([], [])):
            self.assertIsNone(src.fetch_stock_name('600000'))


class _Src(services.DataSource):
    def __init__(self, name, result=None, exc=None):
        self.name = name
        self._result = result
        self._exc = exc
        self.calls = 0

    def _ret(self):
        self.calls += 1
        if self._exc:
            raise self._exc
        return self._result

    def fetch_daily(self, *a):
        return self._ret()

    def fetch_minute(self, *a):
        return self._ret()

    def fetch_stock_name(self, code):
        return self._ret()


@patch('stocks.services.time.sleep')
class TrySourcesTests(SimpleTestCase):
    """多源 failover：异常/空结果切下一个源，退避节奏 1s,1s,2s（DAT-03 / ARC-03 现状锁定）"""

    def test_switches_on_error_and_empty(self, sleep):
        ok = pd.DataFrame({'x': [1]})
        a, b, c = _Src('a', exc=RuntimeError('boom')), _Src('b', pd.DataFrame()), _Src('c', ok)
        src, res = services._try_sources('fetch_daily', '000001', 's', 'e', sources=[a, b, c])
        self.assertIs(src, c)
        self.assertIs(res, ok)
        self.assertEqual([s.calls for s in (a, b, c)], [1, 1, 1])
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_all_fail_returns_none_pair(self, sleep):
        srcs = [_Src('a', exc=RuntimeError('x')), _Src('b', None)]
        self.assertEqual(services._try_sources('fetch_daily', '1', sources=srcs), (None, None))
        self.assertEqual(sleep.call_count, 1)  # 最后一个源之后不再等待

    def test_minute_out_of_range_counts_as_failure(self, _sleep):
        """DAT-01：新浪分钟线全是别的交易日时视为该源失败，换下一个源"""
        other_day = pd.DataFrame({'时间': ['2026-09-07 10:00:00'], '收盘': [1]})
        today = pd.DataFrame({'时间': ['2026-09-08 10:00:00'], '收盘': [2]})
        a, b = _Src('sina', other_day), _Src('bs', today)
        src, res = services._try_sources(
            'fetch_minute', '000001', '2026-09-08 09:30:00', '2026-09-08 15:00:00', sources=[a, b])
        self.assertIs(src, b)
        self.assertEqual(len(res), 1)

    def test_fetch_stock_info_falls_back_to_code(self, _sleep):
        with patch('stocks.ingest.NAME_SOURCES', [_Src('a', None)]):
            self.assertEqual(services.fetch_stock_info('000001'), '000001')
        with patch('stocks.ingest.NAME_SOURCES', [_Src('a', '平安银行')]):
            self.assertEqual(services.fetch_stock_info('000001'), '平安银行')


def _daily_df(rows):
    return pd.DataFrame(rows, columns=['日期', '开盘', '收盘', '最高', '最低', '成交量', '成交额'])


@patch('stocks.services.time.sleep')
class FetchDailyDataTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(code='600000', name='浦发银行')
        self.src = services.DataSource()
        self.src.name = 'sina'

    def test_all_sources_down_returns_zero(self, _sleep):
        with patch('stocks.ingest._try_sources', return_value=(None, None)):
            self.assertEqual(services.fetch_daily_data(self.stock), 0)
        self.assertFalse(DailyQuote.objects.exists())

    def test_parses_rows_chains_prev_close_and_skips_nan(self, _sleep):
        df = _daily_df([
            ['2026-09-07', 10.0, 10.0, 10.5, 9.5, 100, 1000.0],
            ['2026-09-08', 10.0, 11.0, 11.5, 9.8, None, None],
            ['2026-09-04', float('nan'), 9.0, 9.5, 8.5, 1, 1.0],  # 价格缺失跳过
        ])
        with patch('stocks.ingest._try_sources', return_value=(self.src, df)):
            n = services.fetch_daily_data(self.stock, start_date='20260901', end_date='20260908')
        self.assertEqual(n, 2)
        q7 = DailyQuote.objects.get(trade_date=date(2026, 9, 7))
        q8 = DailyQuote.objects.get(trade_date=date(2026, 9, 8))
        self.assertIsNone(q7.prev_close)  # 库里没有更早一根
        self.assertEqual(q8.prev_close, Decimal('10.000'))
        self.assertEqual(q8.change_pct, Decimal('10.0000'))
        self.assertIsNone(q8.volume)
        self.assertIsNone(q8.turnover)
        self.assertEqual(q8.source, 'sina')

    def test_prev_close_falls_back_to_db_row(self, _sleep):
        DailyQuote.objects.create(
            stock=self.stock, trade_date=date(2026, 9, 1),
            open_price=8, close_price=8, high_price=8, low_price=8,
            open_close_diff=0, open_close_pct=0, high_low_diff=0, high_low_pct=0)
        df = _daily_df([['2026-09-08', 10.0, 8.8, 11.0, 9.0, 1, 1.0]])
        with patch('stocks.ingest._try_sources', return_value=(self.src, df)):
            services.fetch_daily_data(self.stock, start_date='20260908', end_date='20260908')
        q = DailyQuote.objects.get(trade_date=date(2026, 9, 8))
        self.assertEqual(q.prev_close, Decimal('8.000'))
        self.assertEqual(q.change_pct, Decimal('10.0000'))

    def test_full_refetch_starts_from_earliest_row(self, _sleep):
        DailyQuote.objects.create(
            stock=self.stock, trade_date=date(2020, 1, 2),
            open_price=8, close_price=8, high_price=8, low_price=8,
            open_close_diff=0, open_close_pct=0, high_low_diff=0, high_low_pct=0)
        df = _daily_df([['2026-09-08', 10.0, 10.0, 10.0, 10.0, 1, 1.0]])
        with patch('stocks.ingest._try_sources', return_value=(self.src, df)) as ts:
            services.fetch_daily_data(self.stock, full=True)
        self.assertEqual(ts.call_args.args[2], '20200102')


class SaveMinuteBarsTests(TestCase):
    """DAT-03：同一交易日只保留一种周期；只写新增/变化的 bar（PERF-01）"""

    def setUp(self):
        self.stock = Stock.objects.create(code='600000', name='浦发银行')

    def _df(self, minutes, close=10.0):
        return pd.DataFrame({
            '时间': [pd.Timestamp(f'2026-09-08 09:{m:02d}:00') for m in minutes],
            '开盘': close, '收盘': close, '最高': close + 0.1, '最低': close - 0.1,
            '成交量': 100, '成交额': 1000.0,
        })

    def test_idempotent_and_skips_nan_rows(self):
        df = self._df([31, 32])
        df.loc[1, '开盘'] = float('nan')
        self.assertEqual(services._save_minute_bars(self.stock, df, D, 'em', 1), 1)
        self.assertEqual(services._save_minute_bars(self.stock, df, D, 'em', 1), 0)
        self.assertEqual(MinuteBar.objects.count(), 1)

    def test_one_minute_replaces_five_minute_but_not_vice_versa(self):
        services._save_minute_bars(self.stock, self._df([35]), D, 'baostock', 5)
        self.assertEqual(services._save_minute_bars(self.stock, self._df([31, 32]), D, 'em', 1), 2)
        self.assertEqual(set(MinuteBar.objects.values_list('freq', flat=True)), {1})
        self.assertEqual(services._save_minute_bars(self.stock, self._df([40]), D, 'baostock', 5), 0)
        self.assertFalse(MinuteBar.objects.filter(freq=5).exists())

    def test_empty_after_filter_returns_zero(self):
        df = pd.DataFrame({'时间': [None], '开盘': [1], '收盘': [1], '最高': [1], '最低': [1]})
        self.assertEqual(services._save_minute_bars(self.stock, df, D), 0)


@patch('stocks.services.time.sleep')
class FetchMinuteDataTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(code='600000', name='浦发银行')
        DailyQuote.objects.create(
            stock=self.stock, trade_date=D,
            open_price=10, close_price=10, high_price=11, low_price=9,
            open_close_diff=0, open_close_pct=0, high_low_diff=2, high_low_pct=0)
        self.src = services.DataSource()
        self.src.name = 'em'

    def _df(self, rows):
        return pd.DataFrame(rows, columns=['时间', '开盘', '收盘', '最高', '最低', '成交量', '成交额'])

    def test_updates_high_low_time_from_same_day_bars_only(self, _sleep):
        df = self._df([
            ['2026-09-07 10:00:00', 10, 10, 99, 1, 1, 1],  # 别的交易日：不得参与极值
            ['2026-09-08 09:31:00', 10, 10, 10.5, 9.5, 1, 1],
            ['2026-09-08 10:15:00', 10, 10, 10.9, 9.9, 1, 1],
            ['2026-09-08 14:00:00', 10, 10, 10.2, 9.1, 1, 1],
        ])
        with patch('stocks.ingest._try_sources', return_value=(self.src, df)):
            res = services.fetch_minute_data(self.stock, trade_date='2026-09-08')
        self.assertEqual(res['high_price'], Decimal('10.9'))
        self.assertEqual(res['low_price'], Decimal('9.1'))
        q = DailyQuote.objects.get(stock=self.stock, trade_date=D)
        self.assertEqual(timezone.localtime(q.high_time).time().hour, 10)
        self.assertEqual(timezone.localtime(q.low_time).time().hour, 14)

    def test_no_data_or_other_day_only_returns_none(self, _sleep):
        with patch('stocks.ingest._try_sources', return_value=(None, None)):
            self.assertIsNone(services.fetch_minute_data(self.stock, trade_date='2026-09-08'))
        df = self._df([['2026-09-07 10:00:00', 10, 10, 11, 9, 1, 1]])
        with patch('stocks.ingest._try_sources', return_value=(self.src, df)):
            self.assertIsNone(services.fetch_minute_data(self.stock, trade_date='2026-09-08'))

    def test_coarse_source_keeps_existing_one_minute_times(self, _sleep):
        services._save_minute_bars(self.stock, self._df([
            [pd.Timestamp('2026-09-08 09:31:00'), 10, 10, 10.5, 9.5, 1, 1]]), D, 'em', 1)
        bs = services.BaoStockSource()
        df = self._df([['2026-09-08 09:35:00', 10, 10, 12, 8, 1, 1]])
        with patch('stocks.ingest._try_sources', return_value=(bs, df)):
            self.assertIsNone(services.fetch_minute_data(self.stock, trade_date='2026-09-08'))
        self.assertIsNone(DailyQuote.objects.get(trade_date=D).high_time)

    def test_missing_daily_row_still_returns_times(self, _sleep):
        DailyQuote.objects.all().delete()
        df = self._df([['2026-09-08 09:31:00', 10, 10, 10.5, 9.5, 1, 1]])
        with patch('stocks.ingest._try_sources', return_value=(self.src, df)):
            res = services.fetch_minute_data(self.stock, trade_date='2026-09-08')
        self.assertIsNotNone(res['high_time'])


@patch('stocks.services.time.sleep')
class FetchOrchestrationTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(code='600000', name='600000')

    def test_all_data_fills_name_and_skips_non_trading_days(self, _sleep):
        today = date(2026, 10, 12)  # 周一；10/10、10/11 为周末
        with patch('stocks.jobs.fetch_stock_info', return_value='浦发银行'), \
                patch('stocks.jobs.fetch_daily_data', return_value=3) as daily, \
                patch('stocks.jobs.fetch_minute_data') as minute, \
                patch('stocks.trading_calendar.is_trading_day', side_effect=lambda d=None: d.weekday() < 5), \
                patch('django.utils.timezone.localdate', return_value=today):
            n = services.fetch_stock_all_data(self.stock, light=True)
        self.assertEqual(n, 3)
        daily.assert_called_once()
        self.assertEqual([c.kwargs['trade_date'] for c in minute.call_args_list],
                         ['2026-10-12', '2026-10-09'])
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.name, '浦发银行')

    def test_all_active_and_keep_fresh_isolate_failures(self, _sleep):
        Stock.objects.create(code='000001', name='平安银行')
        Stock.objects.create(code='000002', name='万科A', is_active=False)

        def fake(stock, light=True):
            if stock.code == '000001':
                raise RuntimeError('boom')
            return 7

        with patch('stocks.jobs.fetch_stock_all_data', side_effect=fake):
            for fn in (services.fetch_all_active_stocks, services.fetch_keep_fresh):
                res = {r['code']: r for r in fn()}
                self.assertEqual(set(res), {'600000', '000001'})
                self.assertEqual(res['600000']['count'], 7)
                self.assertEqual(res['000001']['status'], 'error')

    def test_incremental_daily_counts_ok_and_fail(self, _sleep):
        Stock.objects.create(code='000001', name='平安银行')
        with patch('stocks.jobs.fetch_daily_data', side_effect=[5, RuntimeError('x')]):
            self.assertEqual(services.fetch_incremental_daily_all(), {'ok': 1, 'fail': 1})

    def test_intraday_update_guards(self, _sleep):
        with patch('stocks.trading_calendar.is_trading_time', return_value=False), \
                patch('stocks.jobs.fetch_minute_data') as minute:
            services.fetch_intraday_update()
        minute.assert_not_called()
        # 日历未知（None）时写库任务保守跳过
        with patch('stocks.trading_calendar.is_trading_time', return_value=True), \
                patch('stocks.trading_calendar.is_trading_day', return_value=None), \
                patch('stocks.jobs.fetch_minute_data') as minute:
            services.fetch_intraday_update()
        minute.assert_not_called()
        with patch('stocks.trading_calendar.is_trading_time', return_value=True), \
                patch('stocks.trading_calendar.is_trading_day', return_value=True), \
                patch('stocks.jobs.fetch_minute_data', side_effect=RuntimeError('x')) as minute:
            services.fetch_intraday_update()  # 单只失败不抛
        minute.assert_called_once()

    def test_daily_summary_guard_and_window(self, _sleep):
        with patch('stocks.trading_calendar.is_trading_day', return_value=False), \
                patch('stocks.jobs.fetch_daily_data') as daily:
            services.fetch_daily_summary()
        daily.assert_not_called()
        with patch('stocks.trading_calendar.is_trading_day', return_value=True), \
                patch('stocks.jobs.fetch_daily_data') as daily, \
                patch('stocks.jobs.fetch_minute_data') as minute:
            services.fetch_daily_summary()
        start = datetime.strptime(daily.call_args.kwargs['start_date'], '%Y%m%d').date()
        end = datetime.strptime(daily.call_args.kwargs['end_date'], '%Y%m%d').date()
        self.assertEqual(end - start, timedelta(days=3))
        minute.assert_called_once()


class StockSearchTests(SimpleTestCase):
    def setUp(self):
        services._stock_list_cache.update({'df': None, 'expires': 0})
        self.addCleanup(services._stock_list_cache.update, {'df': None, 'expires': 0})
        self.df = pd.DataFrame({'code': ['000001', '600000', '601318'],
                                'name': ['平安银行', '浦发银行', '中国平安']})

    def test_search_by_name_and_code_literal(self):
        with patch('akshare.stock_info_a_code_name', return_value=self.df) as api:
            self.assertEqual([r['code'] for r in services.search_stocks('平安')], ['000001', '601318'])
            self.assertEqual([r['code'] for r in services.search_stocks('6000')], ['600000'])
            self.assertEqual(services.search_stocks('.*'), [])  # 按字面匹配
            self.assertEqual(services.search_stocks('平'), [])  # 少于 2 字
        api.assert_called_once()  # 列表缓存

    def test_upstream_failure_keeps_old_list_or_empty(self):
        with patch('akshare.stock_info_a_code_name', side_effect=RuntimeError('down')):
            self.assertEqual(services.search_stocks('平安'), [])
        services._stock_list_cache.update({'df': self.df, 'expires': 0})
        with patch('akshare.stock_info_a_code_name', side_effect=RuntimeError('down')):
            self.assertEqual(len(services.search_stocks('平安')), 2)
