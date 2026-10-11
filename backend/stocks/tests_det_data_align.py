"""探测器取数层（data-align）回归测试；各用例类注明对应条目编号。"""

import random
from datetime import date, datetime, time as dt_time, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from .detector import dimensions
from .detector.constants import CONTEXT_BARS
from .models import MarketDailySnapshot

_TODAY = timezone.localdate()


def _weekday(day=None):
    """测试日历：周一至周五为交易日。"""
    return day.weekday() < 5


def _always(day=None):
    return True


def _corr(xs, ys):
    pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxy = sum((x - mx) * (y - my) for x, y in pts)
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    syy = sum((y - my) ** 2 for _, y in pts)
    return sxy / (sxx * syy) ** 0.5


class TradingDayAxisTests(TestCase):
    """#8：日期轴由交易日历生成，不再「最近 N 行」。"""

    def test_axis_skips_weekends_and_keeps_gaps(self):
        end = date(2026, 10, 9)  # 周五
        with patch('stocks.trading_calendar.is_trading_day', _weekday):
            axis = dimensions.trading_day_axis(end, 6)
        self.assertEqual(axis, [date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6),
                                date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9)])

    def test_known_data_day_counts_even_if_calendar_unknown(self):
        end = date(2026, 10, 9)
        known = [date(2026, 10, 9), date(2026, 10, 7)]
        with patch('stocks.trading_calendar.is_trading_day', lambda day=None: None):
            axis = dimensions.trading_day_axis(end, 5, known)
        # 日历全未知：退化为已有数据的日期（与旧的按行取数等价）
        self.assertEqual(axis, sorted(known))


class SectorAxisTests(TestCase):
    """#8/#10：板块快照按交易日轴对齐，整天缺快照为 None 而不是压缩相邻。"""

    def _seed(self, days, skip=()):
        for i in range(days):
            day = _TODAY - timedelta(days=days - 1 - i)
            if i in skip:
                continue
            MarketDailySnapshot.objects.create(
                kind=MarketDailySnapshot.KIND_INDUSTRY_FF, trade_date=day,
                payload=[{'name': '甲', 'net': 1e8 * (i % 3 - 1), 'change_pct': 0.1 * (i % 5)}],
            )

    def test_missing_snapshot_day_is_none(self):
        self._seed(30, skip=(20,))
        with patch('stocks.trading_calendar.is_trading_day', _always):
            ctx, err = dimensions.build_sector_context('industry', '甲')
        self.assertEqual(err, '')
        self.assertEqual(len(ctx['dates']), 30)  # 缺口日仍在轴上
        gap = (_TODAY - timedelta(days=9)).isoformat()
        i = ctx['dates'].index(gap)
        self.assertIsNone(ctx['net'][i])
        self.assertIsNone(ctx['change_pct'][i])

    def test_axis_starts_at_first_snapshot_and_caps_length(self):
        self._seed(30)
        with patch('stocks.trading_calendar.is_trading_day', _always):
            snaps = dimensions.load_sector_snapshots('industry')
            short = dimensions.load_sector_snapshots('industry', lookback_days=10)
        self.assertEqual(len(snaps), 30)  # 按现有可得长度，不在头部堆空日
        self.assertEqual(len(short), 10)
        self.assertEqual(short[-1][0], _TODAY)


class MarginSeriesTests(TestCase):
    """#9：融资余额用 rz、按交易日对齐、末点 = 指数数据日的前一交易日。"""

    def _seed(self, days, skip=(), bad=()):
        for i in range(days):
            day = _TODAY - timedelta(days=days - i)  # 最后一行为 T−1
            if i in skip:
                continue
            rz = 18000.0 + 10 * i
            MarketDailySnapshot.objects.create(
                kind=MarketDailySnapshot.KIND_MARGIN, trade_date=day,
                payload=[{'total': rz * 1.01, 'rz': rz / 2 if i in bad else rz, 'sz_total': 1.0}],
            )

    def test_series_uses_rz_and_ends_at_previous_trading_day(self):
        self._seed(10)
        MarketDailySnapshot.objects.create(  # T 当天的数扫描时尚未披露，不得混入
            kind=MarketDailySnapshot.KIND_MARGIN, trade_date=_TODAY,
            payload=[{'total': 1.0, 'rz': 0.99, 'sz_total': 0.5}])
        with patch('stocks.trading_calendar.is_trading_day', _always):
            items, as_of = dimensions.load_margin_series(_TODAY)
        self.assertEqual(items[-1]['date'], (_TODAY - timedelta(days=1)).isoformat())
        self.assertEqual(items[-1]['rz'], 18090.0)
        self.assertEqual(as_of, items[-1]['date'])
        self.assertEqual(len(items), 10)

    def test_previous_day_not_disclosed_leaves_last_point_none(self):
        self._seed(10, skip=(9,))
        with patch('stocks.trading_calendar.is_trading_day', _always):
            items, as_of = dimensions.load_margin_series(_TODAY)
        self.assertEqual(items[-1]['date'], (_TODAY - timedelta(days=1)).isoformat())
        self.assertIsNone(items[-1]['rz'])
        self.assertEqual(as_of, (_TODAY - timedelta(days=2)).isoformat())

    def test_gap_and_bad_rz_become_none(self):
        self._seed(10, skip=(4,), bad=(6,))
        with patch('stocks.trading_calendar.is_trading_day', _always):
            items, _ = dimensions.load_margin_series(_TODAY)
        self.assertEqual(len(items), 10)  # 缺口日留在轴上
        self.assertIsNone(items[4]['rz'])
        self.assertIsNone(items[6]['rz'])  # 单边按 0 相加的旧行（比值异常）不冒充

    def test_eval_unavailable_when_recent_window_has_gap(self):
        from .detector.registry import DIMENSION_MAP

        params = dict(DIMENSION_MAP['margin_north']['defaults'])
        margin = [{'date': str(i), 'rz': 1.0e4 * (1 + 0.0005 * i)} for i in range(40)]
        ctx = {'kind': 'index', 'code': 'sh000300', 'margin': margin}
        self.assertTrue(dimensions.eval_margin_north(ctx, params)['available'])
        margin[-2]['rz'] = None  # 3 日均值窗口不齐：严格不算，不跳过缺口硬凑
        self.assertFalse(dimensions.eval_margin_north(ctx, params)['available'])


class MarginSnapshotRowTests(TestCase):
    """#9：两融快照行的融资余额要两市齐全才合计。"""

    def test_snapshot_row_rz_requires_both_markets(self):
        from .market.snapshots import margin_snapshot_row

        row = margin_snapshot_row(_TODAY, {'total': 10.0, 'rz': 9.8}, {'total': 8.0, 'rz': None})
        self.assertIsNone(row['rz'])
        row = margin_snapshot_row(_TODAY, {'total': 10.0, 'rz': 9.8}, {'total': 8.0, 'rz': 7.9})
        self.assertEqual(row['rz'], 17.7)
        self.assertEqual(row['total'], 18.0)


class BackfillMarginCommandTests(TestCase):
    """#9：backfill_margin 沪市批量 + 深市按日，失败可续跑。"""

    START, END = date(2026, 9, 28), date(2026, 10, 2)  # 周一至周五

    def _sh(self, start=None, end=None):
        out, day = {}, start
        while day <= end:
            out[day.strftime('%Y%m%d')] = {'total': 100.0, 'rz': 99.0, 'rq': 1.0}
            day += timedelta(days=1)
        return out

    def _run(self, sz_side_effect):
        out = StringIO()
        with patch('stocks.trading_calendar.is_trading_day', _weekday), \
                patch('stocks.market.sentiment._sh_margin_rows', side_effect=self._sh), \
                patch('stocks.market.sentiment._sz_margin_row', side_effect=sz_side_effect):
            call_command('backfill_margin', '--start', self.START.isoformat(),
                         '--end', self.END.isoformat(), '--sleep', '0', stdout=out)
        return out.getvalue()

    def _count(self):
        return MarketDailySnapshot.objects.filter(kind=MarketDailySnapshot.KIND_MARGIN).count()

    def test_partial_failure_then_resume(self):
        def flaky(day):
            return None if day == date(2026, 9, 30) else {'total': 80.0, 'rz': 79.0}

        with self.assertRaises(CommandError):
            self._run(flaky)
        self.assertEqual(self._count(), 4)
        row = MarketDailySnapshot.objects.get(kind=MarketDailySnapshot.KIND_MARGIN, trade_date=self.START)
        self.assertEqual(row.payload[0]['rz'], 178.0)

        probed = []

        def ok(day):
            probed.append(day)
            return {'total': 80.0, 'rz': 79.0}

        self._run(ok)
        self.assertEqual(self._count(), 5)
        self.assertEqual(probed, [date(2026, 9, 30)])  # 续跑只补缺的那天

    def test_rejects_inverted_range(self):
        with self.assertRaises(CommandError):
            call_command('backfill_margin', '--start', '2026-10-02', '--end', '2026-09-28')


class SectorFlowResidualTests(TestCase):
    """#6：板块资金净额对当日涨跌回归取残差，去掉与价格的机械相关。"""

    def _synthetic(self, n=200, seed=7):
        rng = random.Random(seed)
        pcts = [rng.gauss(0, 1.2) for _ in range(n)]
        # 同花顺口径：净额 ≈ 涨跌幅驱动的主成分 + 与价格无关的资金
        nets = [3e8 * p + rng.gauss(0, 1e8) for p in pcts]
        return nets, pcts

    def test_residual_decorrelates_from_change_pct(self):
        nets, pcts = self._synthetic()
        resid = dimensions.sector_flow_residuals(nets, pcts)
        self.assertGreater(_corr(nets, pcts), 0.9)
        self.assertLess(abs(_corr(resid, pcts)), 0.1)

    def test_residual_has_no_lookahead(self):
        nets, pcts = self._synthetic()
        full = dimensions.sector_flow_residuals(nets, pcts)
        cut = dimensions.sector_flow_residuals(nets[:120], pcts[:120])
        self.assertEqual(full[:120], cut)

    def test_insufficient_points_are_none(self):
        nets, pcts = self._synthetic(n=30)
        pcts[25] = None
        resid = dimensions.sector_flow_residuals(nets, pcts, min_obs=20)
        self.assertTrue(all(v is None for v in resid[:19]))
        self.assertIsNone(resid[25])
        self.assertIsNotNone(resid[29])

    def test_dimension_ignores_purely_price_driven_flow(self):
        from .detector.registry import DIMENSION_MAP

        params = {**DIMENSION_MAP['big_order_net']['defaults'], 'noise_pct': 0}
        rng = random.Random(1)
        n = 90
        pcts = [rng.gauss(0, 0.3) for _ in range(n - 8)] + [0.6] * 8  # 末段温和上涨
        nets = [3e8 * p for p in pcts]  # 净额完全由涨跌决定
        ctx = {'kind': 'industry', 'dates': [str(i) for i in range(n)], 'net': nets,
               'change_pct': pcts}
        raw = dimensions.eval_big_order_net(ctx, params)
        self.assertEqual(raw['direction'], 'up')  # 原始净额：把「涨了」再数一遍
        ctx['flow_resid'] = dimensions.sector_flow_residuals(nets, pcts)
        res = dimensions.eval_big_order_net(ctx, params)
        self.assertIsNone(res['direction'], res)

    def test_sector_context_carries_residual(self):
        for i in range(30):
            MarketDailySnapshot.objects.create(
                kind=MarketDailySnapshot.KIND_CONCEPT_FF, trade_date=_TODAY - timedelta(days=29 - i),
                payload=[{'name': '乙', 'net': 1e8 * ((i * 7) % 5 - 2), 'change_pct': 0.1 * (i % 4)}],
            )
        with patch('stocks.trading_calendar.is_trading_day', _always):
            ctx, _ = dimensions.build_sector_context('concept', '乙')
        self.assertEqual(len(ctx['flow_resid']), len(ctx['net']))
        self.assertIsNotNone(ctx['flow_resid'][-1])


class DetectorSchedulerDependencyTests(TestCase):
    """#14：探测器只等行业 + 概念板块快照，不再等 market_ff。"""

    def _ready(self, kinds, hh=17, mm=15):
        from . import scheduler

        for kind in kinds:
            MarketDailySnapshot.objects.create(kind=kind, trade_date=_TODAY, payload=[])
        at = timezone.make_aware(datetime.combine(_TODAY, dt_time(hh, mm)))
        with patch('stocks.scheduler.timezone.localtime', return_value=at):
            return scheduler._detector_dependencies_ready(_TODAY)

    def test_ready_without_market_ff(self):
        self.assertEqual(self._ready(('industry_ff', 'concept_ff')), (True, 'ready'))

    def test_waits_for_concept_ff(self):
        with self.assertLogs('stocks.scheduler', level='WARNING'):
            self.assertEqual(self._ready(('industry_ff', 'market_ff')), (False, 'waiting'))


class ContextLengthTests(TestCase):
    """#10：个股/指数日线上下文拉长到 CONTEXT_BARS 根（本地有多少用多少）。"""

    def test_stock_context_uses_up_to_context_bars(self):
        from .models import DailyQuote, Stock

        stock = Stock.objects.create(code='600000', name='浦发银行')
        start = date(2025, 1, 1)
        DailyQuote.objects.bulk_create([
            DailyQuote(
                stock=stock, trade_date=start + timedelta(days=i), open_price=Decimal('10'),
                high_price=Decimal('10.2'), low_price=Decimal('9.8'), close_price=Decimal('10'),
                open_close_diff=Decimal('0'), open_close_pct=Decimal('0'),
                high_low_diff=Decimal('0.4'), high_low_pct=Decimal('4'),
                volume=1000000, turnover=Decimal('10000000'), is_final=True,
            )
            for i in range(CONTEXT_BARS + 20)
        ])
        ctx, err = dimensions.build_stock_context(stock)
        self.assertEqual(err, '')
        self.assertEqual(len(ctx['dates']), CONTEXT_BARS)
        self.assertEqual(ctx['dates'][-1], (start + timedelta(days=CONTEXT_BARS + 19)).isoformat())

    def test_index_context_requests_context_bars(self):
        items = [{'date': (date(2025, 1, 1) + timedelta(days=i)).isoformat(), 'open': 1.0,
                  'high': 1.0, 'low': 1.0, 'close': 1.0, 'volume': 1.0} for i in range(40)]
        with patch('stocks.market.indices.fetch_index_daily_series',
                   return_value={'available': True, 'items': items}) as fetch:
            ctx, _ = dimensions.build_index_context('sh000001', '上证指数')
        fetch.assert_called_once_with('sh000001', days=CONTEXT_BARS)
        self.assertEqual(len(ctx['dates']), 40)


class SectorSnapshotPayloadTests(TestCase):
    """#6：板块快照一并落库流入/流出资金，为「净额/成交额」口径积累历史。"""

    def test_inflow_outflow_saved(self):
        from unittest.mock import Mock

        import pandas as pd

        from .market import snapshots

        day = date(2026, 9, 7)
        item = {'name': '半导体', 'net': 1e8, 'change_pct': 1.5, 'inflow': 5e8, 'outflow': 4e8}
        with patch('stocks.trading_calendar.is_trading_day', _always), \
                patch('stocks.market.etf_flow.fetch_flow_klines', new=Mock(side_effect=RuntimeError('offline'))), \
                patch('stocks.market.sectors.fetch_industry_fund_flow', return_value=[item]), \
                patch('stocks.market.sectors.fetch_concept_fund_flow', return_value=[]), \
                patch('stocks.market.etf._normalized_etf_items', return_value=[]), \
                patch('stocks.market.snapshots._etf_spot_fresh_for', return_value=True), \
                patch('akshare.fund_etf_scale_sse', return_value=pd.DataFrame()):
            snapshots.save_daily_snapshots(trade_date=day)
        row = MarketDailySnapshot.objects.get(kind=MarketDailySnapshot.KIND_INDUSTRY_FF, trade_date=day)
        self.assertEqual(row.payload[0]['inflow'], 5e8)
        self.assertEqual(row.payload[0]['outflow'], 4e8)
