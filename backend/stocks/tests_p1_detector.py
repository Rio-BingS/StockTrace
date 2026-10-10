"""P1 探测器回归测试：板块伪价格缺口（ALG-06）、维度开关（ALG-07）、
同日重跑幂等（ALG-08）、数据新鲜度（ALG-11）。

约定与 tests_detector 一致：交易日历 patch 为「每天都交易日」，上游一律 patch，绝不触网。
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase

from .detector import scan, signals
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
