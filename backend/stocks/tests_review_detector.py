"""探测器复盘口径 v2 与自动调参重写的回归测试（#5 #7 #12）。

- #12：阈值 k·σ20·√h、板块 T+1 收盘基期、h 日相对沪深300 超额落库；
- #7：DetectorRun 落库基线/超额命中率/独立样本数，API 返回，旧行为空不报错；
- #5：自动调参默认关闭只演练；开启后跨日累计、按日聚类、Beta 后验显著才调，
  目标值向默认值收缩且有界，调的是 noise_pct 与权重。
"""

from __future__ import annotations

import math
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .detector import review, scan
from .models import (
    DetectorDimConfig,
    DetectorRun,
    DetectorSignal,
    MarketDailySnapshot,
)

_TODAY = timezone.localdate()


def _always_trading(day=None):
    return True


def _seeder():
    from .tests_detector import ReviewTests

    return ReviewTests()


def _configs():
    return {c['key']: c for c in scan.load_configs()}


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
        with patch.object(review, '_benchmark_bars', lambda cache: {}):
            out = review._stock_outcome(sig, {})
        self.assertIsNotNone(out)
        self.assertFalse(out['hit'])
        self.assertGreater(out['thr'], 0.3)


@patch('stocks.trading_calendar.is_trading_day', _always_trading)
class SectorBaseTests(TestCase):
    """#12：板块以 T+1 收盘为基期，T→T+1 的隔夜跳空不再计入兑现。"""

    @staticmethod
    def _no_bench():
        return {('benchmark', review.BENCHMARK_CODE): {}}

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
        out = review._sector_outcome(sig, self._no_bench())
        self.assertIsNotNone(out)
        self.assertFalse(out['hit'])
        self.assertAlmostEqual(out['fwd_return'], 0.0, places=3)

    def test_needs_h_days_after_t1(self):
        # 只有 T+1..T+5：T+1 收盘基期还差最后一天，保持待回看
        sig = self._seed([0.5, 0.6, 0.4, 0.3, 0.2])
        self.assertIsNone(review._sector_outcome(sig, self._no_bench()))

    def test_sector_bench_uses_t1_close_basis(self):
        sig = self._seed([0.0, 1.0, 1.0, 1.0, 1.0, 1.0])
        t1 = sig.trade_date + timedelta(days=1)
        end = sig.trade_date + timedelta(days=6)
        bench = {t1: (90.0, 100.0), end: (101.0, 102.0)}
        out = review._sector_outcome(sig, {('benchmark', review.BENCHMARK_CODE): bench})
        self.assertAlmostEqual(out['bench_return'], 2.0)  # 100 → 102，而不是开盘 90 → 102
        self.assertAlmostEqual(out['excess_return'], out['fwd_return'] - 2.0)

    def test_baseline_uses_same_offset(self):
        # 收盘基期的基线：起点 s 以 s+1 收盘为基期；只要求窗口落在 T 及之前
        from datetime import date

        bars = [(date(2026, 1, 1) + timedelta(days=i), None, 100.0 * (1.01 ** i)) for i in range(40)]
        rate = review._baseline(bars, 'up', 1.0, base_is_open=False)
        self.assertEqual(rate, 1.0)
        self.assertIsNone(review._baseline(bars[:20], 'up', 1.0, base_is_open=False))


@patch('stocks.trading_calendar.is_trading_day', _always_trading)
class ExcessReturnTests(TestCase):
    """#12：h 日相对沪深300 超额落库；基准缺失时留空、不影响判定。"""

    def _bench_for(self, base_date, start_open, end_close):
        return {
            base_date + timedelta(days=1): (start_open, start_open),
            base_date + timedelta(days=review.REVIEW_LOOKBACK): (end_close, end_close),
        }

    def test_review_persists_forward_and_benchmark_returns(self):
        seed = _seeder()
        _s, base_date = seed._make_stock_with_moves('600211', [1.0, 1.5, 2.0, 2.5, 3.0])
        seed._make_signal('600211', base_date)
        bench = self._bench_for(base_date, 4000.0, 4040.0)  # 基准 +1%
        with patch.object(review, '_benchmark_bars', lambda cache: bench):
            review.review_pending(_TODAY, _configs())
        sig = DetectorSignal.objects.get(target_code='600211')
        # 基期 T+1 开盘 10.10，T+5 收盘 10.30
        self.assertAlmostEqual(sig.fwd_return, (10.3 / 10.1 - 1) * 100, places=3)
        self.assertAlmostEqual(sig.bench_return, 1.0, places=3)
        self.assertAlmostEqual(sig.excess_return, sig.fwd_return - 1.0, places=3)
        self.assertEqual(sig.benchmark_code, review.BENCHMARK_CODE)
        self.assertEqual(sig.review_version, review.REVIEW_VERSION)
        self.assertIsNotNone(sig.review_threshold)
        self.assertIsNotNone(sig.review_baseline)

    def test_missing_benchmark_leaves_excess_empty(self):
        seed = _seeder()
        _s, base_date = seed._make_stock_with_moves('600212', [1.0, 1.5, 2.0, 2.5, 3.0])
        seed._make_signal('600212', base_date)
        with patch.object(review, '_benchmark_bars', lambda cache: {}):
            summary = review.review_pending(_TODAY, _configs())
        self.assertEqual(summary['reviewed'], 1)
        sig = DetectorSignal.objects.get(target_code='600212')
        self.assertTrue(sig.review_hit)
        self.assertIsNotNone(sig.fwd_return)
        self.assertIsNone(sig.bench_return)
        self.assertIsNone(sig.excess_return)
        self.assertEqual(sig.benchmark_code, '')

    def test_benchmark_fetch_failure_is_cached_as_empty(self):
        cache = {}
        with patch('stocks.market.indices.fetch_index_daily_series', side_effect=RuntimeError('x')) as m:
            self.assertEqual(review._benchmark_bars(cache), {})
            self.assertEqual(review._benchmark_bars(cache), {})
        self.assertEqual(m.call_count, 1)


class ReviewStatsTests(TestCase):
    """#7：按判定日汇总 基线/超额命中率/独立样本数/方向调整后的相对基准超额。"""

    def _row(self, code, kind='stock', day_offset=10, direction='up', hit=True, base=0.3,
             excess=None, version=2):
        return DetectorSignal.objects.create(
            target_kind=kind, target_code=code, target_name=code,
            trade_date=_TODAY - timedelta(days=day_offset), direction=direction, cycles=2,
            status=DetectorSignal.STATUS_CONFIRMED if hit else DetectorSignal.STATUS_MISSED,
            review_hit=hit, review_date=_TODAY, review_version=version,
            review_baseline=base, excess_return=excess)

    def test_clusters_and_rates(self):
        # 同日同向的 3 只自选股 = 1 个样本；同日的板块与指数各自 1 簇；另一日 1 簇
        self._row('600001', excess=2.0)
        self._row('600002', hit=False, excess=-1.0)
        self._row('600003')
        self._row('甲', kind='industry')
        self._row('乙', kind='concept')
        self._row('sh000001', kind='index', direction='down', hit=False, excess=1.0)
        self._row('600004', day_offset=11)
        stats = review.review_stats(_TODAY)
        self.assertEqual(stats['independent_samples'], 4)
        self.assertAlmostEqual(stats['baseline_rate'], 0.3)
        self.assertAlmostEqual(stats['excess_rate'], round(5 / 7 - 0.3, 3))
        # 向下信号的超额按方向取反：(2 − 1 − 1) / 3
        self.assertAlmostEqual(stats['avg_excess_return'], 0.0)

    def test_legacy_rows_are_not_mixed_in(self):
        self._row('600001', version=None)
        self.assertEqual(review.review_stats(_TODAY), {
            'baseline_rate': None, 'excess_rate': None,
            'independent_samples': None, 'avg_excess_return': None,
        })


@patch('stocks.detector.review._benchmark_bars', lambda cache: {})
@patch('stocks.trading_calendar.is_trading_day', _always_trading)
class AutoTuneTests(TestCase):
    """#5：默认关闭只演练；开启后跨日累计 + 按日聚类 + 显著性 + 收缩 + 有界。"""

    HIT_MOVES = [0.5, 1.2, 2.2, 2.5, 2.8]

    def setUp(self):
        self.seed = _seeder()

    def test_disabled_by_default_only_dry_runs(self):
        self.seed._seed_days('6101', self.HIT_MOVES)
        configs = _configs()  # 种子化配置行
        before = {c.key: (c.weight, dict(c.params or {})) for c in DetectorDimConfig.objects.all()}
        summary = review.review_pending(_TODAY, configs)
        self.assertEqual(summary['adjustments'], [])
        self.assertTrue(summary['tune_stats'])
        after = {c.key: (c.weight, dict(c.params or {})) for c in DetectorDimConfig.objects.all()}
        self.assertEqual(after, before)
        stat = next(x for x in summary['tune_stats'] if x['dim'] == 'vol_ratio_slope')
        self.assertGreaterEqual(stat['prob_above'], review.TUNE_CONFIDENCE)  # 显著但只演练

    @override_settings(DETECTOR_AUTO_TUNE=True)
    def test_same_day_signals_are_one_independent_sample(self):
        self.seed._seed_days('6102', self.HIT_MOVES, same_day=True)
        summary = review.review_pending(_TODAY, _configs())
        self.assertEqual(summary['reviewed'], review.MIN_ADJ_SAMPLES)
        self.assertEqual(summary['adjustments'], [])
        stat = next(s for s in summary['tune_stats'] if s['dim'] == 'vol_ratio_slope')
        self.assertEqual(stat['samples'], 1)

    @override_settings(DETECTOR_AUTO_TUNE=True)
    def test_samples_accumulate_across_days(self):
        self.seed._seed_days('6103', self.HIT_MOVES)  # 信号日 T−10, T−12, …, T−28
        # 较早的扫描日只能回看到最早的 3 个：样本不足，不调
        early = review.review_pending(_TODAY - timedelta(days=19), _configs())
        self.assertEqual(early['reviewed'], 3)
        self.assertEqual(early['adjustments'], [])
        # 今天回看其余 7 个，调参样本 = 跨日累计的 10 个
        today = review.review_pending(_TODAY, _configs())
        self.assertEqual(today['reviewed'], review.MIN_ADJ_SAMPLES - 3)
        entry = next(a for a in today['adjustments'] if a['dim'] == 'vol_ratio_slope')
        self.assertEqual(entry['samples'], review.MIN_ADJ_SAMPLES)

    @override_settings(DETECTOR_AUTO_TUNE=True, DETECTOR_TUNE_WINDOW_DAYS=15)
    def test_samples_outside_window_are_dropped(self):
        self.seed._seed_days('6104', self.HIT_MOVES)
        summary = review.review_pending(_TODAY, _configs())
        self.assertEqual(summary['adjustments'], [])
        stat = next(s for s in summary['tune_stats'] if s['dim'] == 'vol_ratio_slope')
        self.assertLess(stat['samples'], review.MIN_ADJ_SAMPLES)

    @override_settings(DETECTOR_AUTO_TUNE=True)
    def test_target_is_stateless_and_shrunk_toward_default(self):
        self.seed._seed_days('6105', self.HIT_MOVES)
        first = review.review_pending(_TODAY, _configs())
        entry = next(a for a in first['adjustments'] if a['dim'] == 'ma_relation')
        n = entry['samples']
        eff = entry['excess'] * n / (n + review.TUNE_PRIOR_N)
        cfg = DetectorDimConfig.objects.get(key='ma_relation')
        expected_w = round(min(max(math.exp(review.WEIGHT_GAIN * eff), 0.5), 2.0), 3)
        self.assertEqual(cfg.weight, expected_w)
        expected_np = round(min(max(0.8 - 0.5 * eff, 0.6), 0.95), 3)
        self.assertEqual(cfg.params['noise_pct'], expected_np)
        # 同样的证据再评估一次：目标值不变，不会被一路推到边界
        again, _stats = review.tune_dims(_TODAY, _configs())
        self.assertEqual(again, [])
        cfg.refresh_from_db()
        self.assertEqual(cfg.weight, expected_w)

    @override_settings(DETECTOR_AUTO_TUNE=True)
    def test_disabled_gate_is_not_reopened(self):
        DetectorDimConfig.objects.create(key='vol_ratio_slope', name='量比斜率', weight=1.0,
                                         params={'noise_pct': 0})
        self.seed._seed_days('6106', self.HIT_MOVES)
        summary = review.review_pending(_TODAY, _configs())
        entry = next(a for a in summary['adjustments'] if a['dim'] == 'vol_ratio_slope')
        self.assertEqual(set(entry['changes']), {'weight'})
        self.assertEqual(DetectorDimConfig.objects.get(key='vol_ratio_slope').params['noise_pct'], 0)

    @override_settings(DETECTOR_AUTO_TUNE=True)
    def test_dimension_without_gate_only_tunes_weight(self):
        dims = {'vol_compress': {'name': '波动率压缩', 'value': 30, 'direction': 'up',
                                 'weight': 1.0, 'available': True, 'note': ''}}
        for i in range(review.MIN_ADJ_SAMPLES):
            code = f'6107{i:02d}'
            base_date = _TODAY - timedelta(days=10 + 2 * i)
            self.seed._make_stock_with_moves(code, self.HIT_MOVES, base_date=base_date)
            self.seed._make_signal(code, base_date, dims=dims)
        summary = review.review_pending(_TODAY, _configs())
        entry = next(a for a in summary['adjustments'] if a['dim'] == 'vol_compress')
        self.assertEqual(set(entry['changes']), {'weight'})


class BetaPosteriorTests(TestCase):
    """Beta 后验显著性：不引入 scipy，自实现的正则化不完全 Beta 函数。"""

    def test_known_values(self):
        self.assertAlmostEqual(review._betainc(1, 1, 0.3), 0.3, places=10)
        self.assertAlmostEqual(review._betainc(2, 2, 0.5), 0.5, places=10)
        self.assertEqual(review._betainc(3, 4, 0.0), 0.0)
        self.assertEqual(review._betainc(3, 4, 1.0), 1.0)

    def test_matches_binomial_identity_for_integers(self):
        # I_x(a, b) = P(Binom(a+b−1, x) ≥ a)
        for a, b, x in ((3, 5, 0.3), (7, 2, 0.8), (11, 1, 0.4), (4, 9, 0.55)):
            n = a + b - 1
            tail = sum(math.comb(n, j) * x ** j * (1 - x) ** (n - j) for j in range(a, n + 1))
            self.assertAlmostEqual(review._betainc(a, b, x), tail, places=9)

    def test_prob_above(self):
        self.assertGreater(review._prob_above(10, 10, 0.2), 0.99)
        self.assertLess(review._prob_above(0, 10, 0.8), 0.01)
        self.assertAlmostEqual(review._prob_above(5, 10, 0.5), 0.5, places=6)
        # 10 个样本全中、基线 0.95：后验不显著（命中率≈基线）
        self.assertLess(review._prob_above(10, 10, 0.95), review.TUNE_CONFIDENCE)


@patch('stocks.trading_calendar.is_trading_day', _always_trading)
class ReviewApiTests(TestCase):
    """#7：API 返回 命中/基线/超额/样本数；旧行字段为空时照常返回 None。"""

    def test_run_brief_new_fields_and_legacy_nulls(self):
        DetectorRun.objects.create(trade_date=_TODAY, reviewed_count=8, hit_count=4, hit_rate=0.5,
                                   baseline_rate=0.21, excess_rate=0.29, independent_samples=3,
                                   avg_excess_return=1.2)
        DetectorRun.objects.create(trade_date=_TODAY - timedelta(days=1), reviewed_count=5,
                                   hit_count=2, hit_rate=0.4)
        latest = self.client.get(reverse('detector-signals')).json()['latest_run']
        self.assertEqual(latest['baseline_rate'], 0.21)
        self.assertEqual(latest['excess_rate'], 0.29)
        self.assertEqual(latest['independent_samples'], 3)
        self.assertEqual(latest['avg_excess_return'], 1.2)
        self.assertEqual(latest['hit_count'], 4)
        runs = self.client.get(reverse('detector-config')).json()['runs']
        legacy = runs[1]
        for key in ('baseline_rate', 'excess_rate', 'independent_samples', 'avg_excess_return'):
            self.assertIsNone(legacy[key], key)

    def test_signal_row_has_review_details(self):
        DetectorRun.objects.create(trade_date=_TODAY)
        DetectorSignal.objects.create(
            target_kind='stock', target_code='600001', target_name='x', trade_date=_TODAY,
            direction='up', cycles=2, review_version=2, review_threshold=0.45, review_baseline=0.2,
            fwd_return=2.0, bench_return=0.5, excess_return=1.5, benchmark_code='sh000300')
        item = self.client.get(reverse('detector-signals')).json()['items'][0]
        self.assertEqual(item['excess_return'], 1.5)
        self.assertEqual(item['benchmark_code'], 'sh000300')
        self.assertEqual(item['review_version'], 2)

    def test_config_exposes_tune_switch_and_rules(self):
        data = self.client.get(reverse('detector-config')).json()
        rules = data['review_rules']
        self.assertFalse(rules['auto_tune'])
        self.assertEqual(rules['version'], review.REVIEW_VERSION)
        self.assertEqual(rules['benchmark'], 'sh000300')
        self.assertEqual(rules['base']['industry'], 'close_t1')
        self.assertEqual(rules['base']['stock'], 'open_t1')
        self.assertIn('已关闭', data['meta']['disclaimer'])
        with override_settings(DETECTOR_AUTO_TUNE=True):
            data = self.client.get(reverse('detector-config')).json()
        self.assertTrue(data['review_rules']['auto_tune'])
        self.assertNotIn('已关闭', data['meta']['disclaimer'])
