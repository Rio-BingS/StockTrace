"""P0 运维与调度修复的回归测试（OPS-01 / OPS-02 / REL-01 / REL-02）。"""

import gzip
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKUP_SCRIPT = REPO_ROOT / 'deploy' / 'stocktrace-backup.sh'


@unittest.skipUnless(shutil.which('bash') and shutil.which('gzip'), '需要 bash + gzip')
class BackupScriptTests(SimpleTestCase):
    """OPS-01：在线备份脚本能对 WAL 库做一致备份、校验、轮转。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = self.tmp / 'db.sqlite3'
        conn = sqlite3.connect(self.db)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE t (a INTEGER)')
        conn.execute('INSERT INTO t VALUES (42)')
        conn.commit()
        # 故意不关闭连接/不 checkpoint：数据还在 -wal 里，cp 主文件会丢，.backup 不会
        self.addCleanup(conn.close)
        self.bk = self.tmp / 'bk'

    def _run(self, **env):
        full_env = dict(os.environ, STOCKTRACE_DB=str(self.db), BACKUP_DIR=str(self.bk))
        full_env.update(env)
        return subprocess.run(['bash', str(BACKUP_SCRIPT)], env=full_env, capture_output=True, text=True, timeout=60)

    def test_backup_contains_wal_data_and_rotates(self):
        for i in range(3):
            # 文件名精确到秒：预先放两份更早的"旧备份"代替 sleep
            (self.bk).mkdir(exist_ok=True)
            (self.bk / f'db-20000101-00000{i}.sqlite3.gz').write_bytes(gzip.compress(b'old'))
        r = self._run(BACKUP_KEEP='2')
        self.assertEqual(r.returncode, 0, r.stderr)
        files = sorted(p.name for p in self.bk.glob('db-*.sqlite3.gz'))
        self.assertEqual(len(files), 2)
        self.assertNotIn('db-20000101-000000.sqlite3.gz', files)
        newest = self.bk / files[-1]
        restored = self.tmp / 'restored.sqlite3'
        restored.write_bytes(gzip.decompress(newest.read_bytes()))
        rconn = sqlite3.connect(restored)
        self.addCleanup(rconn.close)
        self.assertEqual(rconn.execute('SELECT a FROM t').fetchone()[0], 42)
        self.assertEqual(list(self.bk.glob('.*.partial')), [])

    def test_missing_db_fails_loudly(self):
        r = self._run(STOCKTRACE_DB=str(self.tmp / 'nope.sqlite3'))
        self.assertNotEqual(r.returncode, 0)

    def test_post_hook_failure_is_failure(self):
        r = self._run(BACKUP_POST_HOOK='exit 3')
        self.assertNotEqual(r.returncode, 0)

    def test_bad_keep_rejected(self):
        r = self._run(BACKUP_KEEP='0')
        self.assertNotEqual(r.returncode, 0)
