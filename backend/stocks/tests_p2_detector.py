"""P2 探测器方向回归测试（ALG-04/05/09/10/13/14/15、PERF-04/08/09 等）。

约定与 tests_detector 一致：交易日历 patch 为确定值，上游接口一律 patch，测试不触网。
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from .detector import calm

_TODAY = timezone.localdate()


def _dates(n):
    return [(_TODAY - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def _price_ctx(volumes, closes, highs=None, lows=None, kind='stock'):
    n = len(closes)
    return {
        'kind': kind, 'code': '000001', 'name': '测试', 'dates': _dates(n),
        'open': list(closes), 'close': list(closes),
        'high': highs if highs is not None else [c * 1.01 for c in closes],
        'low': lows if lows is not None else [c * 0.99 for c in closes],
        'volume': volumes, 'as_of': _dates(n)[-1],
    }


class CalmBaselineTests(TestCase):
    """ALG-10：平静判定的基线不含当前样本，当前值与基线同为「窗口齐全」口径。"""

    def _calm_price_ctx(self, n=45):
        volumes = [100 + (2 if i % 2 == 0 else -2) for i in range(n - 5)]
        volumes += [99.0, 101.0, 100.0, 100.5, 100.2]
        closes = [10.0] * n
        # 振幅两两不同，便于断言「当日值不在基线里」
        highs = [10.0 * (1 + 0.01 + i * 0.0001) for i in range(n)]
        lows = [10.0 * (1 - 0.01) for _ in range(n)]
        highs[-1] = 10.0 * 1.01045  # 当日振幅偏低（2.045%，与历史值都不同），应判平静
        return _price_ctx(volumes, closes, highs, lows)

    def test_baselines_exclude_current_sample(self):
        ctx = self._calm_price_ctx()
        seen = []
        real = calm._percentile

        def spy(vals, q):
            seen.append(list(vals))
            return real(vals, q)

        with patch.object(calm, '_percentile', side_effect=spy):
            ok, meta = calm.is_calm_price(ctx)
        self.assertTrue(ok, meta)
        cv_base, amp_base = seen
        amp_now = (ctx['high'][-1] - ctx['low'][-1]) / ctx['close'][-2] * 100
        self.assertNotIn(amp_now, amp_base)
        self.assertEqual(len(amp_base), 43)  # 当日之前的全部振幅（共 44 根振幅，不足 lookback=60）
        # 当前 5 日窗口的 CV 也不在 CV 基线里：基线最后一个窗口止于前一日
        vols = ctx['volume']
        last_base = vols[-6:-1]
        m = sum(last_base) / 5
        expected_last = calm._std(last_base) / m
        self.assertAlmostEqual(cv_base[-1], expected_last)

    def test_missing_today_amplitude_is_not_calm(self):
        # 旧实现先丢 None 再取 amps[-1]，当日缺最高/最低价时拿前一天冒充当日
        ctx = self._calm_price_ctx()
        ctx['high'][-1] = None
        ok, meta = calm.is_calm_price(ctx)
        self.assertFalse(ok)
        self.assertIn('当日振幅缺失', meta['reason'])

    def test_rvol_base_window_with_gap_is_not_computed(self):
        ctx = self._calm_price_ctx()
        ctx['volume'][-3] = None  # 落在 RVOL 的前 10 日基期窗口里
        ok, meta = calm.is_calm_price(ctx)
        self.assertFalse(ok)
        self.assertIn('成交量历史不足', meta['reason'])

    def test_sector_baselines_exclude_current_window(self):
        nets = [3e8 if i % 2 == 0 else -3e8 for i in range(20)]
        pcts = [0.3 + 0.01 * i if i % 2 == 0 else -0.3 for i in range(20)]
        ctx = {'kind': 'industry', 'code': 'x', 'name': 'x', 'dates': _dates(20),
               'net': nets, 'change_pct': pcts, 'as_of': _dates(20)[-1]}
        seen = []
        real = calm._percentile

        def spy(vals, q):
            seen.append(list(vals))
            return real(vals, q)

        with patch.object(calm, '_percentile', side_effect=spy):
            calm.is_calm_sector(ctx)
        flow_base, std_base = seen
        self.assertEqual(len(flow_base), 20 - 3)   # 不含末尾 3 日窗口
        self.assertEqual(len(std_base), 20 - 5)    # 不含末尾 5 日窗口
        self.assertNotIn(calm._std(pcts[-5:]), std_base)


class SignValueBoundaryTests(TestCase):
    """ALG-15：_sign_value 的零值与非法参数边界。"""

    def test_zero_raw_has_no_direction_even_when_min_is_zero(self):
        from .detector.dimensions import _sign_value

        value, direction, _note = _sign_value(0.0, {'mild': 1.0, 'min': 0.0, 'max': 2.0})
        self.assertEqual(value, 0.0)
        self.assertIsNone(direction)

    def test_invalid_params_do_not_vote(self):
        from .detector.dimensions import _sign_value

        for params in (
            {'mild': 0, 'min': 0.1, 'max': 2.0},
            {'mild': -1, 'min': 0.1, 'max': 2.0},
            {'mild': 1.0, 'min': 3.0, 'max': 2.0},
            {'mild': 1.0, 'min': -0.1, 'max': 2.0},
            {'mild': 1.0, 'min': None, 'max': 2.0},
        ):
            value, direction, note = _sign_value(0.5, params)
            self.assertIsNone(direction, params)
            self.assertIsNone(value, params)
            self.assertIn('参数非法', note)
