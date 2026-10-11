"""ARC 第二轮 detector-ai 方向：阈值集中（ARC-06）的回归测试。"""

from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from .detector import calm, constants, dimensions, registry, review, signals


class DetectorConstantsTests(SimpleTestCase):
    """阈值集中到 detector/constants 后数值一律不变，旧模块上的同名引用仍可用。"""

    def test_values_unchanged(self) -> None:
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

    def test_old_module_names_point_to_constants(self) -> None:
        self.assertIs(calm.PRICE_CALM_PARAMS, constants.PRICE_CALM_PARAMS)
        self.assertIs(calm.SECTOR_CALM_PARAMS, constants.SECTOR_CALM_PARAMS)
        self.assertIs(signals.SEVERITY_RULES, constants.SEVERITY_RULES)
        self.assertEqual(signals.VOTE_MIN_DIMS, constants.VOTE_MIN_DIMS)
        for name in ('REVIEW_LOOKBACK', 'HIT_SIGMA_K', 'SIGMA_WINDOW', 'HIT_PCT', 'MIN_ADJ_SAMPLES',
                     'WEIGHT_BOUNDS', 'MIN_SCALE_BOUNDS', 'WEIGHT_BAND', 'EXCESS_LOW', 'EXCESS_HIGH'):
            self.assertEqual(getattr(review, name), getattr(constants, name), name)
        for name in ('MIN_PRICE_BARS', 'MIN_SECTOR_POINTS', 'MARGIN_VOTE_CODES', 'SECTOR_LOOKBACK_DAYS'):
            self.assertEqual(getattr(dimensions, name), getattr(constants, name), name)

    def test_registry_defaults_unchanged(self) -> None:
        spec = registry.DIMENSION_MAP
        self.assertEqual(spec['vol_compress']['defaults']['compress_pct'], 0.25)
        self.assertEqual(spec['turnover_pct']['defaults']['split_ratio'], 1.2)
        for key in ('big_order_net', 'vol_ratio_slope', 'turnover_pct', 'margin_north', 'ma_relation'):
            for k, v in constants.NOISE_DEFAULTS.items():
                self.assertEqual(spec[key]['defaults'][k], v, (key, k))
        self.assertEqual(spec['vol_compress']['defaults']['noise_window'], 60)
        self.assertEqual(spec['vol_compress']['defaults']['noise_min'], 15)
        self.assertNotIn('noise_pct', spec['vol_compress']['defaults'])


class AiQuotesFinalOnlyTests(TestCase):
    """AI 取行情只用已收盘日线：盘中未定稿（is_final=False）的当日行不能当收盘数据喂给 LLM。

    DAT-13 的读取侧已在 3d3c120 修过个股分析；这里把分析消息与选股点评两条
    取数路径一起锁住，防止回归。
    """

    def setUp(self) -> None:
        from .models import DailyQuote, Stock

        self.stock = Stock.objects.create(code='600000', name='回归')
        common = {
            'open_close_diff': Decimal('0.5'), 'open_close_pct': Decimal('5'),
            'high_low_diff': Decimal('0.9'), 'high_low_pct': Decimal('9'),
            'volume': 1, 'turnover': Decimal('1'),
        }
        DailyQuote.objects.create(
            stock=self.stock, trade_date=date(2026, 9, 4), is_final=True,
            open_price=Decimal('10'), close_price=Decimal('10.5'),
            high_price=Decimal('10.8'), low_price=Decimal('9.9'), change_pct=Decimal('5'), **common,
        )
        DailyQuote.objects.create(
            stock=self.stock, trade_date=date(2026, 9, 7), is_final=False,
            open_price=Decimal('10.5'), close_price=Decimal('12.34'),
            high_price=Decimal('12.5'), low_price=Decimal('10.4'), change_pct=Decimal('9.9'), **common,
        )

    def test_analysis_messages_skip_intraday_row(self) -> None:
        from .ai.prompts import build_analysis_messages

        user = build_analysis_messages(self.stock)[-1]['content']
        self.assertIn('最新交易日：2026-09-04', user)
        self.assertNotIn('2026-09-07', user)
        self.assertNotIn('12.34', user)

    def test_comment_rows_use_final_quote(self) -> None:
        from .ai.views import _comment_rows

        rows = _comment_rows([self.stock.code])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['trade_date'], date(2026, 9, 4))
        self.assertEqual(rows[0]['close_price'], Decimal('10.500'))
