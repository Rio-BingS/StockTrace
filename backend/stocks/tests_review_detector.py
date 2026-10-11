"""探测器复盘口径 v2 的回归测试（#12）：阈值 k·σ20·√h、板块 T+1 收盘基期。"""

from __future__ import annotations

import math
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from .detector import review
from .models import DetectorSignal, MarketDailySnapshot

_TODAY = timezone.localdate()


def _always_trading(day=None):
    return True


def _seeder():
    from .tests_detector import ReviewTests

    return ReviewTests()


@patch('stocks.trading_calendar.is_trading_day', _always_trading)
class ThresholdTests(TestCase):
    """#12：兑现阈值按 √h 放大到 h 日尺度，k 可配。"""

    def test_threshold_scales_with_sqrt_horizon(self):
        self.assertAlmostEqual(review._threshold(0.2), 0.2 * math.sqrt(review.REVIEW_LOOKBACK))
        # σ 不可得时以 HIT_PCT 作日波动，同样乘 √h
        self.assertAlmostEqual(review._threshold(None), review.HIT_PCT * math.sqrt(review.REVIEW_LOOKBACK))

    @override_settings(DETECTOR_REVIEW_SIGMA_K=2.0)
    def test_k_comes_from_settings(self):
        self.assertAlmostEqual(review._threshold(0.2), 2.0 * 0.2 * math.sqrt(review.REVIEW_LOOKBACK))

    @override_settings(DETECTOR_REVIEW_SIGMA_K=-1)
    def test_invalid_k_falls_back_to_constant(self):
        self.assertEqual(review._sigma_k(), review.HIT_SIGMA_K)

    def test_one_daily_sigma_move_is_no_longer_a_hit(self):
        # σ20≈0.2：旧口径阈值 0.2%，T+1 后 +0.3% 即兑现；新口径阈值≈0.45%，不兑现
        seed = _seeder()
        _s, base_date = seed._make_stock_with_moves('600201', [0.0, 0.3, 0.3, 0.3, 0.3])
        sig = seed._make_signal('600201', base_date)
        out = review._stock_outcome(sig, {})
        self.assertIsNotNone(out)
        self.assertFalse(out['hit'])
        self.assertGreater(out['thr'], 0.3)


@patch('stocks.trading_calendar.is_trading_day', _always_trading)
class SectorBaseTests(TestCase):
    """#12：板块以 T+1 收盘为基期，T→T+1 的隔夜跳空不再计入兑现。"""

    def _seed(self, after_pcts):
        base_date = _TODAY - timedelta(days=12)
        pcts = [(-0.3 if i % 2 else -0.1) for i in range(40)] + [0.0] + after_pcts
        start = base_date - timedelta(days=40)
        for i, pct in enumerate(pcts):
            MarketDailySnapshot.objects.create(
                kind=MarketDailySnapshot.KIND_INDUSTRY_FF, trade_date=start + timedelta(days=i),
                payload=[{'name': '乙', 'net': 1e8, 'change_pct': pct}])
        return DetectorSignal.objects.create(
            target_kind='industry', target_code='乙', target_name='乙', trade_date=base_date,
            direction='up', cycles=2, status=DetectorSignal.STATUS_ACTIVE)

    def test_overnight_jump_into_t1_close_is_not_counted(self):
        # T+1 当天大涨 3%（旧口径以 T 收盘为基期，直接兑现），之后横盘
        sig = self._seed([3.0, 0.0, 0.05, -0.05, 0.0, 0.0])
        out = review._sector_outcome(sig, {})
        self.assertIsNotNone(out)
        self.assertFalse(out['hit'])

    def test_needs_h_days_after_t1(self):
        # 只有 T+1..T+5：T+1 收盘基期还差最后一天，保持待回看
        sig = self._seed([0.5, 0.6, 0.4, 0.3, 0.2])
        self.assertIsNone(review._sector_outcome(sig, {}))

    def test_baseline_uses_same_offset(self):
        # 收盘基期的基线：起点 s 以 s+1 收盘为基期；只要求窗口落在 T 及之前
        from datetime import date

        bars = [(date(2026, 1, 1) + timedelta(days=i), None, 100.0 * (1.01 ** i)) for i in range(40)]
        rate = review._baseline(bars, 'up', 1.0, base_is_open=False)
        self.assertEqual(rate, 1.0)
        self.assertIsNone(review._baseline(bars[:20], 'up', 1.0, base_is_open=False))
