"""候选维度 vp_corr 与否决/状态开关原型的回归测试（new-dims，00_plan 阶段 4）。

约定同 tests_p2_detector：纯函数测试固定随机种子，结果确定；不触网。
"""

import math
import random
from datetime import date, timedelta

from django.test import SimpleTestCase, TestCase

from .detector import calm, registry
from .detector.dimensions_extra import (
    _corr_z,
    _ranks,
    _spearman,
    apply_vetoes,
    etf_regime,
    etf_share_totals,
    eval_vp_corr,
    load_etf_share_totals,
    veto_holder_surge,
)
from .models import MarketDailySnapshot


def _params(**over):
    return {**registry.DIMENSION_MAP['vp_corr']['defaults'], **over}


def _walk(rng, n=70, sigma=0.015, beta=0.0, k=25, drift=0.0, sync=0.0, vol_sd=0.3):
    """随机游走；末 k 日植入量价关系（同 /tmp 回测原型 sim_vp.walk）。

    beta：昨日量能冲击 → 今日收益（量先于价）；sync：当日量能冲击 → 当日收益（量价同步）。
    """
    p, closes, vols, prev = 10.0, [], [], 0.0
    for i in range(n):
        shock = rng.gauss(0, vol_sd)
        planted = i >= n - k
        r = rng.gauss(0, sigma)
        if planted:
            r += (beta * prev + sync * shock) * sigma / vol_sd + drift
        p *= 1 + r
        closes.append(p)
        vols.append(1e6 * math.exp(shock))
        prev = shock
    dates = [str(i) for i in range(n)]
    return {
        'kind': 'stock', 'code': 'x', 'name': 'x', 'dates': dates,
        'open': list(closes), 'close': closes,
        'high': [c * 1.01 for c in closes], 'low': [c * 0.99 for c in closes],
        'volume': vols, 'as_of': dates[-1], 'amount': None, 'float_shares': None,
    }


class RankCorrHelperTests(SimpleTestCase):
    def test_ranks_average_ties(self):
        self.assertEqual(_ranks([3, 1, 3, 2]), [3.5, 1.0, 3.5, 2.0])

    def test_spearman_monotone_and_constant(self):
        self.assertAlmostEqual(_spearman([1, 2, 3, 4], [10, 20, 30, 45]), 1.0)
        self.assertAlmostEqual(_spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)
        self.assertIsNone(_spearman([1, 2, 3], [5, 5, 5]))

    def test_corr_z_units_and_clip(self):
        # 零相关 → 0；完全相关被截断为有限值；样本 ≤3 不可算
        self.assertEqual(_corr_z(0.0, 20), 0.0)
        self.assertTrue(math.isfinite(_corr_z(1.0, 20)))
        self.assertGreater(_corr_z(0.5, 20), 1.64)
        self.assertLess(_corr_z(-0.5, 20), -1.64)
        self.assertIsNone(_corr_z(0.5, 3))
        self.assertIsNone(_corr_z(None, 20))


class VpCorrEvalTests(SimpleTestCase):
    def test_result_shape_matches_other_dimensions(self):
        res = eval_vp_corr(_walk(random.Random(1)), _params())
        self.assertEqual(set(res), {'available', 'value', 'direction', 'note', 'series'})
        self.assertTrue(res['available'])
        self.assertTrue(res['series'])

    def test_short_history_unavailable(self):
        res = eval_vp_corr(_walk(random.Random(1), n=25), _params())
        self.assertFalse(res['available'])
        self.assertIsNone(res['direction'])

    def test_missing_volume_inside_window_unavailable(self):
        ctx = _walk(random.Random(1))
        ctx['volume'][-5] = None
        res = eval_vp_corr(ctx, _params())
        self.assertFalse(res['available'])

    def test_amount_used_when_volume_absent(self):
        ctx = _walk(random.Random(1))
        ctx['amount'], ctx['volume'] = list(ctx['volume']), None
        self.assertTrue(eval_vp_corr(ctx, _params())['available'])

    def _first_vote(self, beta, seeds=range(40)):
        for seed in seeds:
            res = eval_vp_corr(_walk(random.Random(seed), beta=beta), _params())
            if res['direction'] is not None:
                return res
        return None

    def test_planted_volume_lead_votes_up_and_negative_lead_votes_down(self):
        up = self._first_vote(1.0)
        self.assertIsNotNone(up)
        self.assertEqual(up['direction'], 'up')
        self.assertGreater(up['value'], 0)
        down = self._first_vote(-1.0)
        self.assertIsNotNone(down)
        self.assertEqual(down['direction'], 'down')
        self.assertLess(down['value'], 0)

    def test_planted_lead_has_power_noise_does_not(self):
        def rate(beta, n=200):
            rng = random.Random(11)
            return sum(eval_vp_corr(_walk(rng, beta=beta), _params())['direction'] is not None
                       for _ in range(n)) / n
        self.assertLess(rate(0.0), 0.10)
        self.assertGreater(rate(1.0), 0.25)

    def test_sync_stronger_than_lead_does_not_vote(self):
        # 同时植入强同步与领先：领先 z 过了温和带，但同步更强 → 不投票
        results = [eval_vp_corr(_walk(random.Random(seed), beta=0.6, sync=2.0), _params())
                   for seed in range(60)]
        synced = [r for r in results if '量价同步' in r['note']]
        self.assertTrue(synced)
        self.assertTrue(all(r['direction'] is None for r in synced))

    def test_price_already_moved_does_not_vote(self):
        notes = [eval_vp_corr(_walk(random.Random(s), beta=1.0, drift=0.012), _params())
                 for s in range(40)]
        moved = [r for r in notes if '价格已明显移动' in r['note']]
        self.assertTrue(moved)
        self.assertTrue(all(r['direction'] is None for r in moved))

    def test_noise_gate_history_requirement(self):
        res = eval_vp_corr(_walk(random.Random(3), beta=1.0), _params(noise_min=500))
        self.assertIsNone(res['direction'])

    def test_invalid_params_do_not_vote(self):
        res = eval_vp_corr(_walk(random.Random(3), beta=1.0), _params(mild=0))
        self.assertIsNone(res['direction'])


class VpCorrNoiseSignalTests(SimpleTestCase):
    """启用 vp_corr 后，随机游走下「≥2 维同向」信号率仍须 < 5%（同 ALG-04 守护口径）。

    生成器与种子沿用 tests_p2_detector 的随机游走守护测试，便于对照；另要求
    vp_corr 带来的增量 ≤ 2 个百分点（实测约 +1.0~1.4pp）。
    """

    def _rates(self, sigma, with_vp):
        from .tests_p2_detector import _random_walk_ctx

        specs = [s for s in registry.DIMENSIONS
                 if (s['enabled'] or (with_vp and s['key'] == 'vp_corr')) and 'stock' in s['targets']]
        rng = random.Random(7)
        fired = fired_calm = calm_n = 0
        n = 800
        for _ in range(n):
            ctx = _random_walk_ctx(rng, sigma=sigma)
            up = down = 0
            for spec in specs:
                d = spec['evaluate'](ctx, dict(spec['defaults']))['direction']
                up += d == 'up'
                down += d == 'down'
            fire = (up >= 2 and not down) or (down >= 2 and not up)
            fired += fire
            if calm.is_calm(ctx)[0]:
                calm_n += 1
                fired_calm += fire
        return fired / n, fired_calm / max(calm_n, 1)

    def test_signal_rate_with_vp_corr_enabled_stays_below_5pct(self):
        for sigma in (0.008, 0.015, 0.025):
            overall, given_calm = self._rates(sigma, True)
            base, _ = self._rates(sigma, False)
            self.assertLess(overall, 0.05, sigma)
            self.assertLess(given_calm, 0.05, sigma)
            self.assertLessEqual(overall - base, 0.02, sigma)


class VpCorrRegistryTests(SimpleTestCase):
    def test_registered_disabled_with_reason(self):
        spec = registry.DIMENSION_MAP['vp_corr']
        self.assertFalse(spec['enabled'])
        self.assertTrue(spec['disabled_reason'])
        self.assertIs(spec['evaluate'], eval_vp_corr)
        self.assertEqual(spec['targets'], ('index', 'stock'))
        self.assertNotIn('vp_corr', registry.ENABLED_KEYS)
        self.assertNotIn('vp_corr', [s['key'] for s in registry.specs_for_target('stock')])

    def test_appended_after_existing_entries(self):
        # registry 多方并行修改：新条目只追加在末尾，不重排既有维度
        self.assertEqual(registry.DIMENSIONS[-1]['key'], 'vp_corr')


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
