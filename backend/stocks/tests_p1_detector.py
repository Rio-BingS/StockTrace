"""P1 探测器回归测试：板块伪价格缺口（ALG-06）、维度开关（ALG-07）、
同日重跑幂等（ALG-08）、数据新鲜度（ALG-11）。

约定与 tests_detector 一致：交易日历 patch 为「每天都交易日」，上游一律 patch，绝不触网。
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase

from .detector import dimensions, scan, signals
from .models import DetectorDimConfig, DetectorSignal
from .tests_detector import _TODAY, _trading_true, _vote_config


class DimensionEnabledTests(TestCase):
    """ALG-07：DB 停用的维度不评估、不投票。"""

    def _ctx(self):
        return {'kind': 'stock', 'code': '600000', 'name': '600000', 'as_of': _TODAY.isoformat()}

    def test_disabled_dim_does_not_vote(self):
        configs = [
            _vote_config('a', {'600000': (30.0, 'up')}),
            _vote_config('b', {'600000': (40.0, 'up')}),
        ]
        configs[1]['enabled'] = False
        signal_data, dims = signals.evaluate_target(self._ctx(), configs)
        self.assertIsNone(signal_data)  # 只剩 1 票，不够出信号
        self.assertNotIn('b', dims)

    def test_load_configs_respects_db_switch(self):
        scan.load_configs()
        DetectorDimConfig.objects.filter(key='ma_relation').update(is_enabled=False)
        cfgs = {c['key']: c for c in scan.load_configs()}
        self.assertFalse(cfgs['ma_relation']['enabled'])

        def boom(ctx, params):
            raise AssertionError('停用维度不应被评估')

        cfgs['ma_relation']['evaluate'] = boom
        ctx = {'kind': 'industry', 'code': 'x', 'name': 'x', 'as_of': _TODAY.isoformat(),
               'dates': [], 'net': [], 'change_pct': []}
        with patch('stocks.services.is_trading_day', _trading_true):
            _sig, dims = signals.evaluate_target(ctx, list(cfgs.values()))
        self.assertNotIn('ma_relation', dims)
        # 注册表中无实现的停用维度同样不出现（不会以「维度计算异常」冒出来）
        self.assertNotIn('order_imbalance', dims)


class SectorPriceGapTests(TestCase):
    """ALG-06：板块涨跌幅缺一天不再让 ma_relation 整窗失效。"""

    def _ctx(self, pcts):
        dates = [(_TODAY - timedelta(days=len(pcts) - 1 - i)).isoformat() for i in range(len(pcts))]
        return {'kind': 'industry', 'code': '测试行业', 'name': '测试行业', 'dates': dates,
                'net': [1e8] * len(pcts), 'change_pct': pcts, 'as_of': dates[-1]}

    def _pcts(self, n=45):
        return [0.3 if i % 2 == 0 else -0.25 for i in range(n)]

    def test_gap_before_window_keeps_dimension_available(self):
        params = registry_params('ma_relation')
        pcts = self._pcts()
        pcts[5] = None  # 缺口在均线窗口之前
        res = dimensions.eval_ma_relation(self._ctx(pcts), params)
        self.assertTrue(res['available'], res['note'])
        # level 跨缺口保持：窗口全在缺口之后时，结果与该日涨跌 0% 的完整序列一致
        filled = list(pcts)
        filled[5] = 0.0
        clean = dimensions.eval_ma_relation(self._ctx(filled), params)
        self.assertAlmostEqual(res['value'], clean['value'], places=6)

    def test_price_series_keeps_level_across_gap(self):
        out = dimensions._sector_price_series({'change_pct': [1.0, None, 1.0]})
        self.assertIsNone(out[1])
        self.assertAlmostEqual(out[2], 100 * 1.01 * 1.01)

    def test_gap_inside_window_is_honestly_unavailable(self):
        pcts = self._pcts()
        pcts[-3] = None
        res = dimensions.eval_ma_relation(self._ctx(pcts), registry_params('ma_relation'))
        self.assertFalse(res['available'])
        self.assertNotIn('异常', res['note'])


def registry_params(key):
    from .detector import registry

    return dict(registry.DIMENSION_MAP[key]['defaults'])


def _scan_patches(targets=None, ctx_builder=None, calm_result=(False, {'reason': '不平静'})):
    """run_scan 的公共打桩：固定收盘后时钟、关掉两融与指数上游。"""
    from contextlib import ExitStack

    from .tests_detector import _after_close_clock

    stack = ExitStack()
    stack.enter_context(patch('stocks.services.is_trading_day', _trading_true))
    stack.enter_context(patch.object(scan, 'timezone', _after_close_clock()))
    stack.enter_context(patch.object(scan, 'save_margin_snapshot', return_value=0))
    stack.enter_context(patch.object(scan, 'build_index_context', return_value=(None, '指数日线不可用')))
    if targets is not None:
        stack.enter_context(patch.object(scan, '_collect_targets', return_value=targets))
    if ctx_builder is not None:
        stack.enter_context(patch.object(scan, '_build_context', side_effect=ctx_builder))
    if calm_result is not None:
        stack.enter_context(patch.object(scan.calm_mod, 'is_calm', return_value=calm_result))
    return stack


class FreshnessGateTests(TestCase):
    """ALG-11：最新数据日早于扫描交易日的对象跳过并记 degraded。"""

    def test_stale_target_is_degraded_and_not_evaluated(self):
        targets = [
            {'kind': 'stock', 'code': '000001', 'name': '停牌股'},
            {'kind': 'stock', 'code': '000002', 'name': '正常股'},
        ]
        as_of = {'000001': _TODAY - timedelta(days=30), '000002': _TODAY}

        def build(t):
            return {'kind': t['kind'], 'code': t['code'], 'as_of': as_of[t['code']].isoformat()}, ''

        with _scan_patches(targets, build):
            summary = scan.run_scan()
        self.assertEqual(summary['targets_scanned'], 1)
        from .models import DetectorRun

        reasons = {d['target']: d['reason'] for d in DetectorRun.objects.get(trade_date=_TODAY).degraded}
        self.assertIn('数据过期', reasons['stock:000001'])
        self.assertNotIn('stock:000002', reasons)

    def test_sector_missing_today_snapshot_produces_no_old_dated_signal(self):
        from .models import MarketDailySnapshot
        from .tests_detector import SectorSignalScanTests

        seeder = SectorSignalScanTests()
        seeder.setUp()
        seeder._seed_full_history()
        MarketDailySnapshot.objects.filter(trade_date=_TODAY).delete()
        with _scan_patches(calm_result=None):
            scan.run_scan()
        self.assertFalse(DetectorSignal.objects.filter(target_kind='industry').exists())
        from .models import DetectorRun

        reasons = {d['target']: d['reason'] for d in DetectorRun.objects.get(trade_date=_TODAY).degraded}
        self.assertIn('数据过期', reasons['industry:测试行业'])
