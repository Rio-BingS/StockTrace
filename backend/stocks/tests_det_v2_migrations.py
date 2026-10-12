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
