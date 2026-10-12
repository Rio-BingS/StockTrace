"""det-v2 新增迁移（0022 起）可正反执行：回退到 0021 再前进到最新。"""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

_BEFORE = ('stocks', '0021_detector_dim_config_refresh')


class DetV2MigrationTests(TransactionTestCase):
    def _migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([target])
        return executor

    def _latest(self):
        executor = MigrationExecutor(connection)
        return executor.loader.graph.leaf_nodes('stocks')[0]

    def _tables(self):
        return set(connection.introspection.table_names())

    def tearDown(self):
        # 回到最新状态，不影响其他测试
        self._migrate(self._latest())

    def test_backward_then_forward(self):
        latest = self._latest()
        self._migrate(_BEFORE)
        tables = self._tables()
        self.assertNotIn('stocks_stockmargindaily', tables)
        self.assertNotIn('stocks_stockrestrictedrelease', tables)
        with connection.cursor() as cur:
            cols = {c.name for c in connection.introspection.get_table_description(cur, 'stocks_detectorsignal')}
        self.assertNotIn('annotations', cols)

        self._migrate(latest)
        tables = self._tables()
        self.assertIn('stocks_stockmargindaily', tables)
        self.assertIn('stocks_stockrestrictedrelease', tables)
        with connection.cursor() as cur:
            sig_cols = {c.name for c in connection.introspection.get_table_description(cur, 'stocks_detectorsignal')}
            run_cols = {c.name for c in connection.introspection.get_table_description(cur, 'stocks_detectorrun')}
        self.assertIn('annotations', sig_cols)
        self.assertIn('vetoed', run_cols)

    def test_0025_removes_vp_corr_config_with_audit_log(self):
        latest = self._latest()
        executor = self._migrate(('stocks', '0024_detector_annotations_vetoed'))
        apps = executor.loader.project_state([('stocks', '0024_detector_annotations_vetoed')]).apps
        cfg = apps.get_model('stocks', 'DetectorDimConfig')
        cfg.objects.create(key='vp_corr', name='量价相关性', weight=1.2, params={'window': 20}, is_enabled=False)
        cfg.objects.create(key='ma_relation', name='价格与均线关系')
        executor = self._migrate(latest)
        apps = executor.loader.project_state([latest]).apps
        cfg = apps.get_model('stocks', 'DetectorDimConfig')
        log = apps.get_model('stocks', 'DetectorTuneLog')
        self.assertEqual(list(cfg.objects.values_list('key', flat=True)), ['ma_relation'])
        entry = log.objects.get(dim_key='vp_corr')
        self.assertEqual(entry.source, 'legacy')
        self.assertEqual(entry.changes['removed'][0]['weight'], 1.2)
        # 反向迁移可执行（不重建已删除维度的配置行）
        self._migrate(('stocks', '0024_detector_annotations_vetoed'))
