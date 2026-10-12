"""否决/状态开关原型的回归测试（new-dims，00_plan 阶段 4）。

约定同 tests_p2_detector：纯函数测试固定随机种子，结果确定；不触网。
"""

import random
from datetime import date, timedelta

from django.test import SimpleTestCase, TestCase

from .detector.dimensions_extra import (
    apply_vetoes,
    etf_regime,
    etf_share_totals,
    load_etf_share_totals,
    veto_holder_surge,
)
from .models import MarketDailySnapshot


class HolderSurgeVetoTests(SimpleTestCase):
    def _ctx(self, rows, as_of='2026-05-10'):
        return {'kind': 'stock', 'as_of': as_of, 'holder_counts': rows}

    def test_surge_vetoes_up_only(self):
        res = veto_holder_surge(self._ctx([{'date': '2026-01-20', 'count': 10000},
                                           {'date': '2026-04-25', 'count': 11500}]))
        self.assertTrue(res['available'])
        self.assertEqual(res['veto'], 'up')
        self.assertAlmostEqual(res['change_pct'], 15.0)

    def test_decline_never_vetoes_or_adds(self):
        res = veto_holder_surge(self._ctx([{'date': '2026-01-20', 'count': 10000},
                                           {'date': '2026-04-25', 'count': 8000}]))
        self.assertIsNone(res['veto'])

    def test_future_disclosure_is_ignored(self):
        # 可得日期晚于 as_of 的披露不能用（前视）
        res = veto_holder_surge(self._ctx([{'date': '2026-01-20', 'count': 10000},
                                           {'date': '2026-04-25', 'count': 10100},
                                           {'date': '2026-06-01', 'count': 20000}]))
        self.assertIsNone(res['veto'])

    def test_stale_or_missing_data_unavailable(self):
        stale = veto_holder_surge(self._ctx([{'date': '2025-06-01', 'count': 100},
                                             {'date': '2025-09-01', 'count': 200}]))
        self.assertFalse(stale['available'])
        self.assertFalse(veto_holder_surge({'as_of': '2026-05-10'})['available'])


def _days(n, start=date(2026, 1, 5)):
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


class EtfRegimeTests(SimpleTestCase):
    def _series(self, tail_share_chg, tail_index_chg, n=70, k=5):
        rng = random.Random(5)
        dates = _days(n)
        share, close, totals, closes = 1000.0, 3000.0, [], []
        for i, d in enumerate(dates):
            last = i >= n - k
            share *= 1 + (tail_share_chg / k if last else rng.gauss(0, 0.002))
            close *= 1 + (tail_index_chg / k if last else rng.gauss(0, 0.005))
            totals.append((d, share))
            closes.append((d, close))
        return totals, closes

    def test_support_state_vetoes_down(self):
        res = etf_regime(*self._series(0.08, -0.05))
        self.assertEqual(res['state'], 'support')
        self.assertEqual(res['veto'], 'down')

    def test_cooling_state_vetoes_up(self):
        res = etf_regime(*self._series(-0.08, 0.05))
        self.assertEqual(res['state'], 'cooling')
        self.assertEqual(res['veto'], 'up')

    def test_same_direction_share_and_index_is_neutral(self):
        res = etf_regime(*self._series(0.08, 0.05))
        self.assertEqual(res['state'], 'neutral')
        self.assertIsNone(res['veto'])

    def test_short_history_unavailable(self):
        totals, closes = self._series(0.08, -0.05, n=20)
        self.assertFalse(etf_regime(totals, closes)['available'])

    def test_totals_require_every_watched_code(self):
        rows = [(date(2026, 1, 5), [{'code': 'a', 'share': 1.0}, {'code': 'b', 'share': 2.0}]),
                (date(2026, 1, 6), [{'code': 'a', 'share': 1.5}])]
        self.assertEqual(etf_share_totals(rows, ('a', 'b')), [('2026-01-05', 3.0), ('2026-01-06', None)])


class EtfShareLoaderTests(TestCase):
    def test_load_totals_from_snapshots_ascending(self):
        for i, s in enumerate((10.0, 12.0)):
            MarketDailySnapshot.objects.create(
                kind=MarketDailySnapshot.KIND_ETF_SHARE, trade_date=date(2026, 1, 5) + timedelta(days=i),
                payload=[{'code': '510300', 'share': s}, {'code': '510050', 'share': 1.0}])
        self.assertEqual(load_etf_share_totals(('510300', '510050')),
                         [('2026-01-05', 11.0), ('2026-01-06', 13.0)])


class ApplyVetoesTests(SimpleTestCase):
    def test_matching_available_veto_blocks_direction(self):
        v = {'available': True, 'veto': 'up', 'note': '户数激增'}
        self.assertEqual(apply_vetoes('up', [v]), (None, '户数激增'))
        self.assertEqual(apply_vetoes('down', [v]), ('down', ''))
        self.assertEqual(apply_vetoes('up', [{**v, 'available': False}]), ('up', ''))
        self.assertEqual(apply_vetoes(None, [v]), (None, ''))
