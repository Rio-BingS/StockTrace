"""P0 运维与调度修复的回归测试（OPS-01 / OPS-02 / REL-01 / REL-02）。"""

import gzip
import importlib.util
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


@unittest.skipUnless(importlib.util.find_spec('packaging'), '需要 packaging（开发依赖）')
class DependencyPinTests(SimpleTestCase):
    """OPS-02：关键依赖有上界、锁文件全量精确钉版本且与顶层范围一致。"""

    def _reqs(self, name):
        from packaging.requirements import Requirement

        out = {}
        for line in (REPO_ROOT / 'backend' / name).read_text(encoding='utf-8').splitlines():
            line = line.split('#')[0].strip()
            if line and not line.startswith('-'):
                r = Requirement(line)
                out[r.name.lower()] = r
        return out

    def test_top_level_upper_bounds(self):
        reqs = self._reqs('requirements.txt')
        for name in ('django', 'apscheduler', 'pandas', 'djangorestframework', 'gunicorn'):
            ops = {s.operator for s in reqs[name].specifier}
            self.assertTrue(ops & {'<', '=='}, f'{name} 缺上界: {reqs[name]}')
        self.assertFalse(reqs['apscheduler'].specifier.contains('4.0.0'))

    def test_lock_is_exact_and_satisfies_ranges(self):
        lock = self._reqs('requirements.lock')
        for name, r in lock.items():
            specs = list(r.specifier)
            self.assertEqual(len(specs), 1, name)
            self.assertEqual(specs[0].operator, '==', name)
        for name, r in self._reqs('requirements.txt').items():
            self.assertIn(name, lock, f'{name} 未进锁文件')
            pinned = next(iter(lock[name].specifier)).version
            self.assertTrue(r.specifier.contains(pinned, prereleases=True), f'{name}=={pinned} 不满足 {r}')

    def test_installed_apscheduler_is_3x(self):
        import apscheduler

        self.assertEqual(apscheduler.__version__.split('.')[0], '3')


class SchedulerConfigTests(SimpleTestCase):
    """REL-01：任务注册带时区、coalesce 与合理 misfire 宽限；Web 内默认不启动。"""

    def test_jobs_have_timezone_coalesce_and_grace(self):
        from zoneinfo import ZoneInfo

        from stocks import scheduler as sch

        s = sch.build_scheduler()
        jobs = {j.id: j for j in s.get_jobs()}
        self.assertEqual(
            set(jobs),
            {'preopen_incremental', 'intraday_fetch', 'daily_summary', 'post_close_lagging', 'detector_scan',
             'minute_bar_cleanup'},
        )
        for job in jobs.values():
            self.assertTrue(job.coalesce, job.id)
            self.assertEqual(job.max_instances, 1, job.id)
            self.assertGreaterEqual(job.misfire_grace_time, 60, job.id)
            self.assertEqual(job.trigger.timezone, ZoneInfo('Asia/Shanghai'), job.id)
        self.assertEqual(str(s.timezone), 'Asia/Shanghai')
        # 盘中 5 分钟任务的宽限必须短于 5 分钟，避免和下一轮叠加
        self.assertLess(jobs['intraday_fetch'].misfire_grace_time, 300)

    def test_ready_does_not_start_scheduler_by_default(self):
        from unittest import mock

        from django.apps import apps

        cfg = apps.get_app_config('stocks')
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch('stocks.scheduler.start') as m:
            os.environ.pop('STOCKTRACE_RUN_SCHEDULER_IN_WEB', None)
            cfg.ready()
            m.assert_not_called()
            os.environ['STOCKTRACE_RUN_SCHEDULER_IN_WEB'] = '1'
            cfg.ready()
            m.assert_called_once()

    def test_ready_logs_exception_when_start_fails(self):
        from unittest import mock

        from django.apps import apps

        cfg = apps.get_app_config('stocks')
        with mock.patch.dict(os.environ, {'STOCKTRACE_RUN_SCHEDULER_IN_WEB': '1'}), \
                mock.patch('stocks.scheduler.start', side_effect=ImportError('apscheduler 4')), \
                self.assertLogs('stocks.apps', level='ERROR'):
            cfg.ready()

    def test_run_scheduler_command_skipped_by_ready(self):
        # REL-03：启动判定改白名单，run_scheduler 等管理命令进程不会在 ready() 里启动调度器
        from unittest import mock

        from stocks import scheduler as sch

        env = {'STOCKTRACE_RUN_SCHEDULER_IN_WEB': '1', 'STOCKTRACE_SCHEDULER': '1'}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(sch.sys, 'argv', ['manage.py', 'run_scheduler']):
            self.assertFalse(sch._should_start_scheduler())

    def test_lock_path_configurable(self):
        from unittest import mock

        from stocks import scheduler as sch

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        lock = tmp / 'run' / 'scheduler.lock'
        with self.settings(STOCKTRACE_SCHEDULER_LOCK=lock), mock.patch.object(sch, '_lock_fh', None):
            self.assertTrue(sch._acquire_lock())
            try:
                self.assertTrue(lock.exists())
                self.assertEqual(lock.read_text().strip(), str(os.getpid()))
            finally:
                sch._release_lock()


class RunForeverTests(SimpleTestCase):
    """REL-01：独立调度进程启动、等待退出信号、优雅关闭并释放锁；拿不到锁时重试。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lock = self.tmp / 'scheduler.lock'
        ctx = self.settings(STOCKTRACE_SCHEDULER_HEARTBEAT=self.tmp / 'hb.json')
        ctx.enable()
        self.addCleanup(ctx.disable)

    def test_starts_and_stops_cleanly(self):
        import threading
        from unittest import mock

        from stocks import scheduler as sch

        ev = threading.Event()
        started = {}

        real_build = sch.build_scheduler

        def fake_build(*a, **k):
            s = real_build(*a, **k)
            started['s'] = s
            # 启动后立刻请求退出
            threading.Timer(0.2, ev.set).start()
            return s

        with self.settings(STOCKTRACE_SCHEDULER_LOCK=self.lock), \
                mock.patch.object(sch, 'build_scheduler', side_effect=fake_build):
            self.assertTrue(sch.run_forever(ev, lock_retry_seconds=1))
        self.assertFalse(started['s'].running)
        from stocks.scheduler import read_heartbeat

        self.assertFalse(read_heartbeat()['running'])  # 正常退出时心跳标记为已停止
        self.assertIsNone(sch.scheduler)
        self.assertIsNone(sch._lock_fh)

    def test_retries_when_lock_held_and_exits_on_signal(self):
        import threading
        from unittest import mock

        from stocks import scheduler as sch

        ev = threading.Event()
        threading.Timer(0.3, ev.set).start()
        with self.settings(STOCKTRACE_SCHEDULER_LOCK=self.lock), \
                mock.patch.object(sch, '_acquire_lock', return_value=False) as acq, \
                mock.patch.object(sch, 'build_scheduler') as build, \
                self.assertLogs('stocks.scheduler', level='WARNING'):
            self.assertFalse(sch.run_forever(ev, lock_retry_seconds=0.1))
        self.assertGreaterEqual(acq.call_count, 2)
        build.assert_not_called()

    def test_command_respects_global_switch(self):
        from io import StringIO
        from unittest import mock

        from django.core.management import call_command

        err = StringIO()
        with mock.patch.dict(os.environ, {'STOCKTRACE_SCHEDULER': '0'}), \
                mock.patch('stocks.scheduler.run_forever') as rf:
            call_command('run_scheduler', stderr=err)
        rf.assert_not_called()
        self.assertIn('STOCKTRACE_SCHEDULER', err.getvalue())


class HeartbeatAndHealthTests(SimpleTestCase):
    """REL-02：调度心跳文件 + /api/health/ 报告 DB 与调度器状态。"""

    databases = {'default'}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hb = self.tmp / 'run' / 'scheduler_heartbeat.json'
        ctx = self.settings(STOCKTRACE_SCHEDULER_HEARTBEAT=self.hb, STOCKTRACE_HEARTBEAT_STALE_SECONDS=180)
        ctx.enable()
        self.addCleanup(ctx.disable)

    def _sched_with_heartbeat(self):
        from stocks import scheduler as sch

        return sch.attach_heartbeat(sch.build_scheduler(), mode='process')

    def test_write_and_read_heartbeat(self):
        from stocks import scheduler as sch

        s = self._sched_with_heartbeat()
        self.assertIn(sch.HEARTBEAT_JOB_ID, {j.id for j in s.get_jobs()})
        sch.write_heartbeat(s)
        hb = sch.read_heartbeat()
        self.assertTrue(hb['running'])
        self.assertEqual(hb['mode'], 'process')
        ids = {j['id'] for j in hb['jobs']}
        self.assertIn('daily_summary', ids)
        self.assertNotIn(sch.HEARTBEAT_JOB_ID, ids)
        self.assertEqual(list(self.hb.parent.glob('*.tmp')), [])

    def test_job_events_recorded_and_logged(self):
        from datetime import datetime as dt

        from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED, JobExecutionEvent

        from stocks import scheduler as sch

        s = self._sched_with_heartbeat()
        listener = sch._make_job_listener(s)
        now = dt.now(sch.SCHEDULER_TZ)
        with self.assertLogs('stocks.scheduler', level='WARNING') as cm:
            listener(JobExecutionEvent(EVENT_JOB_ERROR, 'daily_summary', 'default', now, exception=RuntimeError('boom')))
            listener(JobExecutionEvent(EVENT_JOB_MISSED, 'detector_scan', 'default', now))
        self.assertTrue(any('daily_summary' in m for m in cm.output))
        runs = sch.read_heartbeat()['last_runs']
        self.assertEqual(runs['daily_summary']['status'], 'error')
        self.assertIn('boom', runs['daily_summary']['error'])
        self.assertEqual(runs['detector_scan']['status'], 'missed')
        # 重启后沿用最近结果
        sch.attach_heartbeat(sch.build_scheduler(), mode='process')
        self.assertIn('daily_summary', sch._hb_state['last_runs'])

    def test_health_ok_with_fresh_heartbeat(self):
        from unittest import mock

        from stocks import scheduler as sch

        sch.write_heartbeat(self._sched_with_heartbeat())
        with mock.patch.dict(os.environ, {'STOCKTRACE_SCHEDULER': '1'}):
            r = self.client.get('/api/health/')
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body['status'], 'ok')
        self.assertTrue(body['db']['ok'])
        self.assertTrue(body['scheduler']['alive'])
        self.assertTrue(body['scheduler']['jobs'])

    def test_health_degraded_without_or_with_stale_heartbeat(self):
        import json
        from datetime import timedelta
        from unittest import mock

        from django.utils import timezone

        with mock.patch.dict(os.environ, {'STOCKTRACE_SCHEDULER': '1'}):
            r = self.client.get('/api/health/')
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.json()['scheduler']['error'], 'no heartbeat')

            self.hb.parent.mkdir(parents=True, exist_ok=True)
            old = (timezone.now() - timedelta(minutes=10)).isoformat()
            self.hb.write_text(json.dumps({'running': True, 'updated_at': old}), encoding='utf-8')
            r = self.client.get('/api/health/')
            self.assertEqual(r.status_code, 503)
            self.assertFalse(r.json()['scheduler']['alive'])
            self.assertGreaterEqual(r.json()['scheduler']['heartbeat_age_seconds'], 590)

    def test_health_ok_when_scheduler_disabled(self):
        from unittest import mock

        with mock.patch.dict(os.environ, {'STOCKTRACE_SCHEDULER': '0'}):
            r = self.client.get('/api/health/')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['scheduler']['enabled'])

    def test_health_db_failure_is_503_without_detail(self):
        from unittest import mock

        with mock.patch('stocks.health.connection') as conn, self.assertLogs('stocks.health', level='ERROR'):
            conn.cursor.side_effect = RuntimeError('disk I/O error /secret/path')
            r = self.client.get('/api/health/')
        self.assertEqual(r.status_code, 503)
        self.assertNotIn('secret', r.content.decode())


class LoggingConfigTests(SimpleTestCase):
    """REL-02：LOGGING 已配置，stocks.* 的 INFO 不再丢；行情兜底出错写 logger.exception。"""

    def test_stocks_info_reaches_handler(self):
        import logging

        from django.conf import settings

        self.assertIn('stocks', settings.LOGGING['loggers'])
        lg = logging.getLogger('stocks.scheduler')
        self.assertTrue(lg.isEnabledFor(logging.INFO))
        self.assertTrue(logging.getLogger().handlers, 'root 无 handler')

    def test_market_fallback_logs_exception(self):
        from unittest import mock

        from rest_framework.test import APIRequestFactory

        from stocks.views import market as views

        req = APIRequestFactory().get('/api/market/')
        with mock.patch.object(views, 'get_market_overview', side_effect=RuntimeError('upstream down')), \
                self.assertLogs('stocks.views', level='ERROR') as cm:
            r = views.market_overview(req)
        self.assertEqual(r.status_code, 502)
        self.assertTrue(any('Traceback' in m or 'upstream down' in m for m in cm.output))

        req = APIRequestFactory().get('/api/market/sectors/')
        with mock.patch.object(views, 'get_sector_rotation', side_effect=RuntimeError('x')), \
                self.assertLogs('stocks.views', level='ERROR'):
            self.assertEqual(views.market_sectors(req).status_code, 502)

        req = APIRequestFactory().get('/api/market/institutions/')
        with mock.patch.object(views, 'get_institution_holdings', side_effect=RuntimeError('x')), \
                self.assertLogs('stocks.views', level='ERROR'):
            self.assertEqual(views.market_institutions(req).status_code, 502)
