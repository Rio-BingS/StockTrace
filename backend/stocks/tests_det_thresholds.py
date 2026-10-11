"""探测器阈值口径回归测试（#1 / #3 / #10 / #13）。

纯函数测试：不碰库、不触网；蒙特卡洛一律固定随机种子，结果确定。
"""

import random
from statistics import NormalDist

from django.test import SimpleTestCase

from .detector import dimensions as D
from .detector.registry import DIMENSION_MAP


def _params(key, **over):
    return {**DIMENSION_MAP[key]['defaults'], **over}


def _ctx_from_returns(rets_pct, kind='stock'):
    """由逐日涨跌幅（%）合成价格类 ctx（量能恒定，振幅 1%）。"""
    p, closes = 10.0, []
    for r in rets_pct:
        p *= 1 + r / 100
        closes.append(p)
    n = len(closes)
    dates = [f'd{i:04d}' for i in range(n)]
    return {
        'kind': kind, 'code': '000001', 'name': 'x', 'dates': dates,
        'open': list(closes), 'close': closes,
        'high': [c * 1.005 for c in closes], 'low': [c * 0.995 for c in closes],
        'volume': [1e6] * n, 'as_of': dates[-1], 'amount': [None] * n, 'float_shares': None,
    }


def _nominal_rate(params):
    """z 区间的名义双侧投票率 2·(Φ(−z_lo) − Φ(−z_hi))。"""
    nd = NormalDist()
    return 2 * (nd.cdf(-params['z_lo']) - nd.cdf(-params['z_hi']))


class HarrellDavisQuantileTests(SimpleTestCase):
    """#10：噪声闸门分位改用 Harrell-Davis 估计。"""

    def test_betainc_matches_closed_form(self):
        # 闭式：I_x(2,3) = 6x² − 8x³ + 3x⁴，x=0.4 时为 0.5248
        self.assertAlmostEqual(D._betainc(2, 3, 0.4), 0.5248, places=10)
        self.assertEqual(D._betainc(2, 3, 0.0), 0.0)
        self.assertEqual(D._betainc(2, 3, 1.0), 1.0)

    def test_large_sample_agrees_with_plain_quantile(self):
        rng = random.Random(1)
        vals = [abs(rng.gauss(0, 1)) for _ in range(3000)]
        self.assertAlmostEqual(D._hd_quantile(vals, 0.8), D._percentile(vals, 0.8), delta=0.03)

    def test_small_sample_threshold_is_more_stable(self):
        # 同一过程的多组 40 个样本：HD 估计的离散度小于线性插值分位
        rng = random.Random(2)
        hd, lin = [], []
        for _ in range(400):
            vals = [abs(rng.gauss(0, 1)) for _ in range(40)]
            hd.append(D._hd_quantile(vals, 0.8))
            lin.append(D._percentile(vals, 0.8))

        def sd(xs):
            m = sum(xs) / len(xs)
            return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5

        self.assertLess(sd(hd), sd(lin))

    def test_edge_cases(self):
        self.assertIsNone(D._hd_quantile([], 0.8))
        self.assertEqual(D._hd_quantile([3.0], 0.8), 3.0)
        self.assertAlmostEqual(D._hd_quantile([2.0] * 10, 0.8), 2.0)


class RelativeCeilingTests(SimpleTestCase):
    """#1：噪声闸门的上限改为 noise_hi × 自身噪声带，绝对 max 不再参与。"""

    base = {'mild': 1.0, 'min': 0.01, 'max': 0.5,
            'noise_pct': 0.8, 'noise_window': 120, 'noise_min': 15, 'noise_hi': 2.5}

    def test_absolute_max_no_longer_blocks_when_inside_own_band(self):
        history = [1.0, -1.0] * 20          # 噪声带 ±1.0
        _v, direction, note = D._gated(1.8, history, self.base)
        # 旧口径：1.8 > max=0.5 判为突变；新口径：1.0 < 1.8 ≤ 2.5 照常投票
        self.assertEqual(direction, 'up', note)

    def test_ceiling_scales_with_own_noise(self):
        history = [1.0, -1.0] * 20
        _v, direction, note = D._gated(-2.6, history, self.base)
        self.assertIsNone(direction)
        self.assertIn('突变', note)
        _v, direction, _n = D._gated(-2.4, history, self.base)
        self.assertEqual(direction, 'down')

    def test_without_noise_hi_absolute_max_still_applies(self):
        # 向前兼容：没有 noise_hi 的旧参数行仍按绝对 max 判断
        params = {k: v for k, v in self.base.items() if k != 'noise_hi'}
        _v, direction, note = D._gated(1.8, [1.0, -1.0] * 20, params)
        self.assertIsNone(direction)
        self.assertIn('温和区间', note)

    def test_long_history_uses_up_to_noise_window(self):
        # 250 根上下文：最近 120 个历史值参与，更早的大噪声已滚出窗口
        history = [5.0] * 100 + [1.0] * 120
        thr, note = D._noise_threshold(history, self.base)
        self.assertEqual(note, '')
        self.assertAlmostEqual(thr, 1.0)


class ZBandTests(SimpleTestCase):
    """#1：z 值投票区间与参数校验。"""

    def test_band(self):
        p = {'z_lo': 1.0, 'z_hi': 2.0, 'z_mild': 2.0}
        self.assertEqual(D._zband(1.5, p)[1], 'up')
        self.assertEqual(D._zband(-1.5, p)[1], 'down')
        self.assertIsNone(D._zband(0.5, p)[1])
        self.assertIsNone(D._zband(2.5, p)[1])
        self.assertAlmostEqual(D._zband(1.5, p)[0], 37.5)

    def test_invalid_params_do_not_vote(self):
        for p in ({'z_lo': 2.0, 'z_hi': 1.0, 'z_mild': 2.0},
                  {'z_lo': 1.0, 'z_hi': 2.0, 'z_mild': 0},
                  {'z_lo': None, 'z_hi': 2.0, 'z_mild': 2.0},
                  {}):
            value, direction, note = D._zband(1.5, p)
            self.assertIsNone(direction)
            self.assertIsNone(value)
            self.assertIn('非法', note)


class VolCompressZTests(SimpleTestCase):
    """#3：t 分母改用压缩窗口之外的长窗口 σ，压缩条件下假阳回到名义值。"""

    def test_null_vote_rate_given_compression_is_near_nominal(self):
        rng = random.Random(3)
        params = _params('vol_compress')
        compressed = votes = 0
        for _ in range(2500):
            ctx = _ctx_from_returns([rng.gauss(0, 1.5) for _ in range(70)])
            res = D.eval_vol_compress(ctx, params)
            if not res['available'] or '未压缩' in res['note']:
                continue
            compressed += 1
            votes += res['direction'] is not None
        rate = votes / compressed
        nominal = _nominal_rate(params)          # z_lo=1.645、z_hi=3 → 约 9.7%
        self.assertGreater(compressed, 400)
        # 旧口径（同窗口 t≥2.13）这里约 28%；新口径应落在名义值附近
        self.assertLess(abs(rate - nominal), 0.04, (rate, nominal, compressed))

    def test_high_volatility_target_can_still_vote(self):
        # σ=4% 的标的：压缩期 5 日漂移约 +22%（z≈2.2），旧绝对 max=6% 一律判突变
        rng = random.Random(4)
        rets = [rng.gauss(0, 4.0) for _ in range(65)] + [3.9, 4.0, 3.95, 4.05, 3.95]
        res = D.eval_vol_compress(_ctx_from_returns(rets), _params('vol_compress'))
        self.assertEqual(res['direction'], 'up', res['note'])

    def test_works_with_250_bars(self):
        rng = random.Random(5)
        rets = [rng.gauss(0, 1.5) for _ in range(245)] + [1.45, 1.5, 1.48, 1.52, 1.47]
        res = D.eval_vol_compress(_ctx_from_returns(rets), _params('vol_compress'))
        self.assertTrue(res['available'], res['note'])
        self.assertEqual(res['direction'], 'up', res['note'])
