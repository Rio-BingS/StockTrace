"""P0 数据入库修复回归测试（DAT-01 / PERF-01 / DAT-03 / DAT-02 / DAT-07）"""

from datetime import date, datetime
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

import pandas as pd
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from . import services
from .models import DailyQuote, MinuteBar, Stock


def _aware(s):
    return timezone.make_aware(datetime.strptime(s, '%Y-%m-%d %H:%M:%S'))


def _sina_minute_df(days):
    """模拟新浪 stock_zh_a_minute：多日 bar 混在一起，列为字符串。

    days: {'YYYY-MM-DD': [(HH:MM, high, low), ...]}
    """
    rows = []
    for d, bars in days.items():
        for hm, high, low in bars:
            rows.append({
                'day': f'{d} {hm}:00', 'open': str(low), 'high': str(high),
                'low': str(low), 'close': str(high), 'volume': '1000', 'amount': '10000',
            })
    return pd.DataFrame(rows)


def _make_quote(stock, d, close=10, **kw):
    fields = dict(
        open_price=Decimal(close), close_price=Decimal(close),
        high_price=Decimal(close), low_price=Decimal(close),
        open_close_diff=Decimal(0), open_close_pct=Decimal(0),
        high_low_diff=Decimal(0), high_low_pct=Decimal(0),
    )
    fields.update(kw)
    return DailyQuote.objects.create(stock=stock, trade_date=d, **fields)


class _NoSleep:
    def setUp(self):
        p = patch('stocks.services.time.sleep')
        p.start()
        self.addCleanup(p.stop)
        super().setUp()


class SinaMinuteDateFilterTests(_NoSleep, TestCase):
    """DAT-01：新浪分钟线必须按请求日期过滤，高低点只用当日 bar。"""

    def setUp(self):
        super().setUp()
        self.stock = Stock.objects.create(code='600000', name='浦发银行')
        self.day = date(2026, 9, 2)
        _make_quote(self.stock, self.day)
        # 前一日的极值（11 / 8）比当日（10.5 / 9.5）更极端：不过滤就会写错
        self.df = _sina_minute_df({
            '2026-09-01': [('09:31', 11.0, 8.0), ('09:32', 10.0, 9.0)],
            '2026-09-02': [('09:31', 10.0, 9.8), ('10:15', 10.5, 9.9), ('14:01', 10.1, 9.5)],
        })

    def _run(self, sources):
        with patch('stocks.services.DATA_SOURCES', sources):
            return services.fetch_minute_data(self.stock, trade_date='2026-09-02')

    def test_sina_fallback_uses_only_requested_day(self):
        with patch('akshare.stock_zh_a_minute', return_value=self.df):
            result = self._run([services.SinaSource()])
        self.assertEqual(result['high_time'], _aware('2026-09-02 10:15:00'))
        self.assertEqual(result['low_time'], _aware('2026-09-02 14:01:00'))
        q = DailyQuote.objects.get(stock=self.stock, trade_date=self.day)
        self.assertEqual(q.high_time, _aware('2026-09-02 10:15:00'))
        self.assertEqual(q.low_time, _aware('2026-09-02 14:01:00'))
        # 只入库当日 3 根，不再写入前一日的 bar
        self.assertEqual(MinuteBar.objects.filter(stock=self.stock).count(), 3)

    def test_empty_after_filter_falls_through_to_next_source(self):
        other_day = _sina_minute_df({'2026-08-28': [('09:31', 11.0, 8.0)]})

        class _Next(services.DataSource):
            name = 'next'
            called = False

            def fetch_minute(self, symbol, start_dt, end_dt):
                _Next.called = True
                return _sina_minute_df({'2026-09-02': [('09:45', 10.2, 9.7)]}).rename(
                    columns={'day': '时间', 'open': '开盘', 'high': '最高', 'low': '最低',
                             'close': '收盘', 'volume': '成交量', 'amount': '成交额'})

        with patch('akshare.stock_zh_a_minute', return_value=other_day):
            result = self._run([services.SinaSource(), _Next()])
        self.assertTrue(_Next.called)
        self.assertEqual(result['high_time'], _aware('2026-09-02 09:45:00'))

    def test_all_sources_out_of_range_returns_none(self):
        other_day = _sina_minute_df({'2026-08-28': [('09:31', 11.0, 8.0)]})
        with patch('akshare.stock_zh_a_minute', return_value=other_day):
            self.assertIsNone(self._run([services.SinaSource()]))
        q = DailyQuote.objects.get(stock=self.stock, trade_date=self.day)
        self.assertIsNone(q.high_time)


class RebuildHighLowTimesCommandTests(TestCase):
    """DAT-01 回刷命令：用已有 MinuteBar 按交易日重算。"""

    def setUp(self):
        self.stock = Stock.objects.create(code='000001', name='平安银行')
        self.other = Stock.objects.create(code='000002', name='万科A')
        wrong = _aware('2026-09-01 09:31:00')
        self.q1 = _make_quote(self.stock, date(2026, 9, 1), high_time=wrong, low_time=wrong)
        self.q2 = _make_quote(self.stock, date(2026, 9, 2), high_time=wrong, low_time=wrong)
        self.q3 = _make_quote(self.other, date(2026, 9, 2), high_time=wrong, low_time=wrong)
        for stock in (self.stock, self.other):
            for s, h, lo in [('2026-09-01 09:31:00', 11, 8), ('2026-09-01 10:00:00', 10, 9),
                             ('2026-09-02 09:31:00', 10, 9.8), ('2026-09-02 10:15:00', 10.5, 9.9),
                             ('2026-09-02 14:01:00', 10.1, 9.5)]:
                MinuteBar.objects.create(stock=stock, datetime=_aware(s), open=h, close=h,
                                         high=h, low=lo, volume=1)

    def test_dry_run_does_not_write(self):
        out = StringIO()
        call_command('rebuild_high_low_times', '--dry-run', stdout=out)
        self.assertIn('更正 2 个', out.getvalue())
        self.q2.refresh_from_db()
        self.assertEqual(self.q2.high_time, _aware('2026-09-01 09:31:00'))

    def test_rebuild_with_code_and_since(self):
        call_command('rebuild_high_low_times', '--code', '000001', '--since', '2026-09-02',
                     stdout=StringIO())
        self.q2.refresh_from_db()
        self.assertEqual(self.q2.high_time, _aware('2026-09-02 10:15:00'))
        self.assertEqual(self.q2.low_time, _aware('2026-09-02 14:01:00'))
        # since 之前与其他股票不动
        self.q3.refresh_from_db()
        self.assertEqual(self.q3.high_time, _aware('2026-09-01 09:31:00'))

    def test_rebuild_all(self):
        call_command('rebuild_high_low_times', stdout=StringIO())
        self.q3.refresh_from_db()
        self.assertEqual(self.q3.high_time, _aware('2026-09-02 10:15:00'))
        self.q1.refresh_from_db()
        self.assertEqual(self.q1.low_time, _aware('2026-09-01 09:31:00'))
        self.assertEqual(self.q1.high_time, _aware('2026-09-01 09:31:00'))




class MinuteBulkUpsertTests(_NoSleep, TestCase):
    """PERF-01：分钟线批量 upsert、只写增量；日线批量 upsert 不覆盖高低点时间。"""

    def setUp(self):
        super().setUp()
        self.stock = Stock.objects.create(code='600000', name='浦发银行')
        _make_quote(self.stock, date(2026, 9, 2))

    def _fetch(self, bars):
        df = _sina_minute_df({'2026-09-02': bars})
        with patch('stocks.services.DATA_SOURCES', [services.SinaSource()]), \
                patch('akshare.stock_zh_a_minute', return_value=df):
            return services.fetch_minute_data(self.stock, trade_date='2026-09-02')

    def test_incremental_only_writes_new_or_changed(self):
        self._fetch([('09:31', 10.0, 9.8), ('09:32', 10.1, 9.9)])
        self.assertEqual(MinuteBar.objects.filter(stock=self.stock).count(), 2)
        with patch.object(MinuteBar.objects, 'bulk_create',
                          wraps=MinuteBar.objects.bulk_create) as bc:
            # 第二轮：09:31 不变，09:32 最后一根更新，新增 09:33
            self._fetch([('09:31', 10.0, 9.8), ('09:32', 10.2, 9.9), ('09:33', 10.3, 10.0)])
        written = bc.call_args.args[0]
        self.assertEqual([timezone.localtime(o.datetime).strftime('%H:%M') for o in written],
                         ['09:32', '09:33'])
        self.assertEqual(MinuteBar.objects.filter(stock=self.stock).count(), 3)
        bar = MinuteBar.objects.get(stock=self.stock, datetime=_aware('2026-09-02 09:32:00'))
        self.assertEqual(bar.high, Decimal('10.20'))

    def test_unchanged_round_writes_nothing(self):
        bars = [('09:31', 10.0, 9.8), ('09:32', 10.1, 9.9)]
        self._fetch(bars)
        with patch.object(MinuteBar.objects, 'bulk_create') as bc:
            self._fetch(bars)
        bc.assert_not_called()

    def test_daily_upsert_keeps_high_low_times(self):
        q = DailyQuote.objects.get(stock=self.stock, trade_date=date(2026, 9, 2))
        q.high_time = _aware('2026-09-02 10:00:00')
        q.save()
        df = pd.DataFrame([
            {'日期': '2026-09-01', '开盘': 9.5, '收盘': 9.9, '最高': 10, '最低': 9.4,
             '成交量': 100, '成交额': 1000},
            {'日期': '2026-09-02', '开盘': 9.9, '收盘': 10.0, '最高': 10.2, '最低': 9.8,
             '成交量': 200, '成交额': 2000},
        ])
        with patch('stocks.services._try_sources', return_value=(services.SinaSource(), df)):
            n = services.fetch_daily_data(self.stock, start_date='20260901', end_date='20260902')
        self.assertEqual(n, 2)
        q.refresh_from_db()
        self.assertEqual(q.high_time, _aware('2026-09-02 10:00:00'))
        self.assertEqual(q.close_price, Decimal('10.00'))
        self.assertEqual(q.prev_close, Decimal('9.90'))


class VolumeUnitTests(_NoSleep, TestCase):
    """DAT-03：成交量统一为股、成交额统一为元，并记录来源与周期。"""

    def setUp(self):
        super().setUp()
        self.stock = Stock.objects.create(code='000001', name='平安银行')

    def _daily(self, source):
        with patch('stocks.services.DATA_SOURCES', [source]):
            services.fetch_daily_data(self.stock, start_date='20260901', end_date='20260901')
        return DailyQuote.objects.get(stock=self.stock, trade_date=date(2026, 9, 1))

    def test_eastmoney_daily_lots_to_shares(self):
        df = pd.DataFrame([{'日期': '2026-09-01', '开盘': 10, '收盘': 10.5, '最高': 10.6,
                            '最低': 9.9, '成交量': 1234, '成交额': 1300000.0}])
        with patch('akshare.stock_zh_a_hist', return_value=df):
            q = self._daily(services.EastMoneySource())
        self.assertEqual(q.volume, 123400)
        self.assertEqual(q.turnover, Decimal('1300000.00'))
        self.assertEqual(q.source, 'eastmoney')

    def test_tencent_amount_is_volume_lots_not_turnover(self):
        # akshare stock_zh_a_hist_tx 的列：date,open,close,high,low,amount(=成交量，手)
        df = pd.DataFrame([{'date': date(2026, 9, 1), 'open': 10, 'close': 10.5,
                            'high': 10.6, 'low': 9.9, 'amount': 5678}])
        with patch('akshare.stock_zh_a_hist_tx', return_value=df):
            q = self._daily(services.TencentSource())
        self.assertEqual(q.volume, 567800)
        self.assertIsNone(q.turnover)
        self.assertEqual(q.source, 'tencent')

    def test_sina_daily_shares_unchanged(self):
        df = pd.DataFrame([{'date': '2026-09-01', 'open': 10, 'high': 10.6, 'low': 9.9,
                            'close': 10.5, 'volume': 123400, 'amount': 1300000.0}])
        with patch('akshare.stock_zh_a_daily', return_value=df):
            q = self._daily(services.SinaSource())
        self.assertEqual(q.volume, 123400)
        self.assertEqual(q.turnover, Decimal('1300000.00'))
        self.assertEqual(q.source, 'sina')

    def test_em_minute_lots_to_shares_and_sina_amount_kept(self):
        _make_quote(self.stock, date(2026, 9, 2))
        em_df = pd.DataFrame([{'时间': '2026-09-02 09:31:00', '开盘': 10, '收盘': 10,
                               '最高': 10.1, '最低': 9.9, '成交量': 12, '成交额': 12000.0}])
        with patch('stocks.services.DATA_SOURCES', [services.EastMoneySource()]), \
                patch('akshare.stock_zh_a_hist_min_em', return_value=em_df):
            services.fetch_minute_data(self.stock, trade_date='2026-09-02')
        bar = MinuteBar.objects.get(stock=self.stock)
        self.assertEqual((bar.volume, bar.turnover, bar.source, bar.freq),
                         (1200, Decimal('12000.00'), 'eastmoney', 1))

        sina_df = _sina_minute_df({'2026-09-02': [('09:31', 10.1, 9.9)]})
        with patch('stocks.services.DATA_SOURCES', [services.SinaSource()]), \
                patch('akshare.stock_zh_a_minute', return_value=sina_df):
            services.fetch_minute_data(self.stock, trade_date='2026-09-02')
        bar.refresh_from_db()
        self.assertEqual((bar.volume, bar.turnover, bar.source), (1000, Decimal('10000.00'), 'sina'))


class MinuteFreqTests(_NoSleep, TestCase):
    """DAT-03：同一交易日只保留一种 bar 周期，1 分钟优先。"""

    def setUp(self):
        super().setUp()
        self.stock = Stock.objects.create(code='000001', name='平安银行')
        _make_quote(self.stock, date(2026, 9, 2))

    class _Bs5(services.DataSource):
        name = 'baostock'
        minute_freq = 5

        def fetch_minute(self, symbol, start_dt, end_dt):
            return pd.DataFrame([
                {'时间': pd.Timestamp('2026-09-02 09:35:00'), '开盘': 10, '收盘': 10,
                 '最高': 10.3, '最低': 9.7, '成交量': 500, '成交额': 5000.0},
                {'时间': pd.Timestamp('2026-09-02 09:40:00'), '开盘': 10, '收盘': 10,
                 '最高': 10.1, '最低': 9.9, '成交量': 500, '成交额': 5000.0},
            ])

    def _fetch(self, source, patcher=None):
        with patch('stocks.services.DATA_SOURCES', [source]):
            if patcher:
                with patcher:
                    return services.fetch_minute_data(self.stock, trade_date='2026-09-02')
            return services.fetch_minute_data(self.stock, trade_date='2026-09-02')

    def test_one_minute_replaces_five_minute_bars(self):
        self._fetch(self._Bs5())
        self.assertEqual(set(MinuteBar.objects.values_list('freq', flat=True)), {5})
        sina_df = _sina_minute_df({'2026-09-02': [('09:31', 10.0, 9.95), ('09:32', 10.05, 9.9)]})
        self._fetch(services.SinaSource(), patch('akshare.stock_zh_a_minute', return_value=sina_df))
        self.assertEqual(set(MinuteBar.objects.values_list('freq', flat=True)), {1})
        self.assertEqual(MinuteBar.objects.count(), 2)

    def test_five_minute_not_written_over_one_minute_day(self):
        sina_df = _sina_minute_df({'2026-09-02': [('09:31', 10.0, 9.95), ('09:32', 10.05, 9.9)]})
        self._fetch(services.SinaSource(), patch('akshare.stock_zh_a_minute', return_value=sina_df))
        q = DailyQuote.objects.get(stock=self.stock, trade_date=date(2026, 9, 2))
        self.assertIsNone(self._fetch(self._Bs5()))
        self.assertEqual(set(MinuteBar.objects.values_list('freq', flat=True)), {1})
        q2 = DailyQuote.objects.get(pk=q.pk)
        self.assertEqual(q2.high_time, q.high_time)
