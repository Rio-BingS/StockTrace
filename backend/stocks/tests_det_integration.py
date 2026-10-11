"""探测器优化集成（feat/det）的回归测试：跨方向衔接点。

- 0021 数据迁移：维度改名、失效/过时参数重置为新默认、权重保留、可回滚；
- settings.DETECTOR_SECTOR_SIGNAL_CAP：环境变量接线，默认 10，0 关闭截断；
"""

from __future__ import annotations

import importlib

from django.apps import apps
from django.conf import settings
from django.test import TestCase, override_settings

from .detector import registry, scan
from .models import DetectorDimConfig, DetectorTuneLog

_MIG = importlib.import_module('stocks.migrations.0021_detector_dim_config_refresh')

# v1.7.0 时代种子化的配置行（旧名、旧默认参数，权重被旧调参改过）
_OLD_ROWS = {
    'big_order_net': ('大单净流入', 1.2, {'window': 15, 'mean_days': 6, 'mild': 0.5, 'min': 0.08,
                                         'max': 2.0, 'noise_pct': 0.8, 'noise_window': 60,
                                         'noise_min': 15}),
    'margin_north': ('融资余额/北向', 1.0, {'mild': 0.5, 'min': 0.06, 'max': 2.0, 'noise_pct': 0.8,
                                           'noise_window': 60, 'noise_min': 15}),
    'ma_relation': ('价格与均线关系', 0.8, {'ma': 20, 'slope_days': 3, 'mild': 0.6, 'min': 0.076,
                                           'max': 3.0, 'max_dist': 8.0, 'noise_pct': 0.8,
                                           'noise_window': 60, 'noise_min': 15}),
    'vol_compress': ('波动率压缩', 1.0, {'window': 5, 'mild': 2.0, 'min': 0.2, 'max': 6.0,
                                        'drift_z': 2.13, 'compress_pct': 0.25, 'noise_pct': 0.8,
                                        'noise_window': 60, 'noise_min': 15}),
}


class DimConfigMigrationTests(TestCase):
    def _seed_old(self):
        for key, (name, weight, params) in _OLD_ROWS.items():
            DetectorDimConfig.objects.create(key=key, name=name, weight=weight, params=params)

    def test_snapshot_matches_registry_defaults(self):
        # 迁移写死的是本次发布的新默认值：与注册表一致，迁移后行为 = 新装实例
        for key, params in _MIG.NEW_PARAMS.items():
            self.assertEqual(params, registry.DIMENSION_MAP[key]['defaults'], key)
        for key, name in _MIG.NEW_NAMES.items():
            self.assertEqual(name, registry.DIMENSION_MAP[key]['name'], key)

    def test_forward_renames_resets_params_and_keeps_weight(self):
        self._seed_old()
        _MIG.refresh_configs(apps, None)
        rows = {c.key: c for c in DetectorDimConfig.objects.all()}
        self.assertEqual(rows['big_order_net'].name, '板块资金净额')
        self.assertEqual(rows['margin_north'].name, '融资余额')
        for key, (_name, weight, _params) in _OLD_ROWS.items():
            self.assertEqual(rows[key].params, _MIG.NEW_PARAMS[key], key)
            self.assertEqual(rows[key].weight, weight, key)
        # 生效参数里不再有被旧值覆盖的 noise_window=60，也没有失效键
        cfgs = {c['key']: c for c in scan.load_configs(create=False)}
        self.assertEqual(cfgs['big_order_net']['params']['noise_window'], 120)
        self.assertNotIn('max_dist', cfgs['ma_relation']['params'])
        self.assertNotIn('noise_pct', cfgs['ma_relation']['params'])
        self.assertEqual(cfgs['margin_north']['name'], '融资余额')
        logs = DetectorTuneLog.objects.filter(reason=_MIG.REASON)
        self.assertEqual(logs.count(), len(_OLD_ROWS))
        # 重复执行：已对齐的行不再改、不再记流水
        _MIG.refresh_configs(apps, None)
        self.assertEqual(DetectorTuneLog.objects.filter(reason=_MIG.REASON).count(), len(_OLD_ROWS))

    def test_backward_restores_old_values(self):
        self._seed_old()
        _MIG.refresh_configs(apps, None)
        _MIG.restore_configs(apps, None)
        for key, (name, weight, params) in _OLD_ROWS.items():
            cfg = DetectorDimConfig.objects.get(key=key)
            self.assertEqual((cfg.name, cfg.weight, cfg.params), (name, weight, params), key)
        self.assertFalse(DetectorTuneLog.objects.filter(reason=_MIG.REASON).exists())

    def test_fresh_install_without_rows_is_noop(self):
        _MIG.refresh_configs(apps, None)
        self.assertFalse(DetectorDimConfig.objects.exists())
        self.assertFalse(DetectorTuneLog.objects.exists())


class SectorSignalCapSettingTests(TestCase):
    def test_default_is_ten(self):
        self.assertEqual(settings.DETECTOR_SECTOR_SIGNAL_CAP, 10)
        self.assertEqual(scan._sector_signal_cap(), 10)

    @override_settings(DETECTOR_SECTOR_SIGNAL_CAP=0)
    def test_zero_disables_truncation(self):
        self.assertEqual(scan._sector_signal_cap(), 0)
        collected = [(('industry', f'b{i}'), {'target_kind': 'industry', 'strength': i, 'status': 'watching'})
                     for i in range(15)]
        kept, cut = scan.truncate_by_strength(collected, scan._sector_signal_cap())
        self.assertEqual((len(kept), len(cut)), (15, 0))
