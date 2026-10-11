"""ARC 第二轮 detector-ai 方向：阈值集中（ARC-06）的回归测试。"""

from django.test import SimpleTestCase

from .detector import calm, constants, dimensions, registry, review, signals


class DetectorConstantsTests(SimpleTestCase):
    """阈值集中到 detector/constants 后数值一律不变，旧模块上的同名引用仍可用。"""

    def test_values_unchanged(self):
        # 逐项写死迁移前的数值：常量被误改时这里先失败
        self.assertEqual(constants.PRICE_CALM_PARAMS, {
            'rvol_window': 10, 'cv_window': 5, 'rvol_min': 0.7, 'rvol_max': 1.5,
            'lookback': 60, 'vol_cv_pct': 0.4, 'amp_pct': 0.4,
        })
        self.assertEqual(constants.SECTOR_CALM_PARAMS, {'window': 5, 'flow_pct': 0.5, 'chg_pct': 0.4})
        self.assertEqual((constants.CALM_MIN_AMP_SAMPLES, constants.SECTOR_FLOW_DAYS), (10, 3))
        self.assertEqual(constants.VOTE_MIN_DIMS, 2)
        self.assertEqual(constants.SEVERITY_RULES, {'high_score': 55.0, 'medium_score': 40.0})
        self.assertEqual(
            (constants.HIGH_SEVERITY_MIN_VOTES, constants.ACTIVE_MIN_CYCLES, constants.CYCLE_LOOKBACK),
            (3, 2, 20),
        )
        self.assertEqual(
            (constants.REVIEW_LOOKBACK, constants.HIT_SIGMA_K, constants.SIGMA_WINDOW, constants.HIT_PCT,
             constants.BASELINE_DAYS, constants.MIN_BASELINE_WINDOWS, constants.REVIEW_MAX_AGE_DAYS,
             constants.MIN_ADJ_SAMPLES, constants.STEP, constants.WEIGHT_BOUNDS, constants.MIN_SCALE_BOUNDS,
             constants.WEIGHT_BAND, constants.EXCESS_LOW, constants.EXCESS_HIGH, constants.SEGMENT_HEAD_CYCLES),
            (5, 1.0, 20, 1.5, 60, 20, 60, 10, 1.05, (0.5, 2.0), (0.5, 2.0), 0.05, -0.15, 0.15, 2),
        )
        self.assertEqual(
            (constants.MIN_PRICE_BARS, constants.MIN_SECTOR_POINTS, constants.MARGIN_VOTE_CODES,
             constants.SECTOR_LOOKBACK_DAYS),
            (30, 12, ('sh000300',), 45),
        )
        self.assertEqual(constants.NOISE_DEFAULTS, {'noise_pct': 0.8, 'noise_window': 60, 'noise_min': 15})
        self.assertEqual((constants.COMPRESS_PCT_DEFAULT, constants.SPLIT_RATIO_DEFAULT), (0.25, 1.2))

    def test_old_module_names_point_to_constants(self):
        self.assertIs(calm.PRICE_CALM_PARAMS, constants.PRICE_CALM_PARAMS)
        self.assertIs(calm.SECTOR_CALM_PARAMS, constants.SECTOR_CALM_PARAMS)
        self.assertIs(signals.SEVERITY_RULES, constants.SEVERITY_RULES)
        self.assertEqual(signals.VOTE_MIN_DIMS, constants.VOTE_MIN_DIMS)
        for name in ('REVIEW_LOOKBACK', 'HIT_SIGMA_K', 'SIGMA_WINDOW', 'HIT_PCT', 'MIN_ADJ_SAMPLES',
                     'WEIGHT_BOUNDS', 'MIN_SCALE_BOUNDS', 'WEIGHT_BAND', 'EXCESS_LOW', 'EXCESS_HIGH'):
            self.assertEqual(getattr(review, name), getattr(constants, name), name)
        for name in ('MIN_PRICE_BARS', 'MIN_SECTOR_POINTS', 'MARGIN_VOTE_CODES', 'SECTOR_LOOKBACK_DAYS'):
            self.assertEqual(getattr(dimensions, name), getattr(constants, name), name)

    def test_registry_defaults_unchanged(self):
        spec = registry.DIMENSION_MAP
        self.assertEqual(spec['vol_compress']['defaults']['compress_pct'], 0.25)
        self.assertEqual(spec['turnover_pct']['defaults']['split_ratio'], 1.2)
        for key in ('big_order_net', 'vol_ratio_slope', 'turnover_pct', 'margin_north', 'ma_relation'):
            for k, v in constants.NOISE_DEFAULTS.items():
                self.assertEqual(spec[key]['defaults'][k], v, (key, k))
        self.assertEqual(spec['vol_compress']['defaults']['noise_window'], 60)
        self.assertEqual(spec['vol_compress']['defaults']['noise_min'], 15)
        self.assertNotIn('noise_pct', spec['vol_compress']['defaults'])


class AiSerializersMoveTests(SimpleTestCase):
    """ARC-07：ai_serializers 迁到 stocks.ai.serializers，旧路径是同一个模块对象。"""

    def test_old_path_is_alias(self):
        import stocks.ai_serializers as old
        from stocks.ai import serializers as new

        self.assertIs(old, new)
        self.assertIs(old.AiProviderSerializer, new.AiProviderSerializer)
        self.assertIs(old.first_error_text, new.first_error_text)
