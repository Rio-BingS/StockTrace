"""P1 调度可靠性与性能修复的回归测试（REL-03 / REL-04 / REL-05 / PERF-02 / PERF-03 / PERF-05）。"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from . import task_lock, tasks
from .task_lock import TaskLock


class _TmpRunDirMixin:
    """把 STOCKTRACE_RUN_DIR 指到临时目录，锁文件不落到仓库里。"""

    def setUp(self):
        super().setUp()
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir, True)
        ctx = override_settings(STOCKTRACE_RUN_DIR=self.run_dir)
        ctx.enable()
        self.addCleanup(ctx.disable)


_HOLDER = r'''
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_CREAT, 0o664)
fcntl.flock(fd, fcntl.LOCK_EX)
print('locked', flush=True)
time.sleep(float(sys.argv[2]))
'''


class TaskLockTests(_TmpRunDirMixin, SimpleTestCase):
    """REL-04：按名进程内锁 + 文件锁，可跨线程释放，目录不可用时退化。"""

    def test_same_name_exclusive_and_timeout(self):
        a, b = TaskLock('t_excl'), TaskLock('t_excl')
        self.assertTrue(a.acquire(timeout=0))
        try:
            self.assertFalse(b.acquire(timeout=0))
            t0 = time.monotonic()
            self.assertFalse(b.acquire(timeout=0.3))
            self.assertGreaterEqual(time.monotonic() - t0, 0.25)
            self.assertTrue(TaskLock('t_other').acquire(timeout=0))  # 不同名互不影响
            TaskLock('t_other').release()
        finally:
            a.release()
        self.assertTrue(b.acquire(timeout=0))
        b.release()
        self.assertTrue((self.run_dir / 'task-t_excl.lock').exists())

    def test_release_from_other_thread(self):
        lock = TaskLock('t_xthread')
        self.assertTrue(lock.acquire(timeout=0))
        th = threading.Thread(target=lock.release)
        th.start()
        th.join(5)
        self.assertTrue(TaskLock('t_xthread').acquire(timeout=0))
        TaskLock('t_xthread').release()

    def test_blocked_by_other_process(self):
        if task_lock.fcntl is None:
            self.skipTest('平台无 fcntl')
        path = task_lock.lock_path('t_proc')
        p = subprocess.Popen([sys.executable, '-c', _HOLDER, path, '1.5'], stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.kill)
        self.assertEqual(p.stdout.readline().strip(), 'locked')
        lock = TaskLock('t_proc')
        self.assertFalse(lock.acquire(timeout=0))
        self.assertFalse(lock.locked())  # 失败时进程内锁也已归还
        self.assertTrue(lock.acquire(timeout=10))  # 对方退出后限时等待能拿到
        lock.release()
        p.wait(5)

    def test_degrades_to_thread_lock_when_dir_unusable(self):
        bad = self.run_dir / 'not_a_dir'
        bad.write_text('x')
        with override_settings(STOCKTRACE_RUN_DIR=bad), self.assertLogs('stocks.task_lock', level='WARNING'):
            lock = TaskLock('t_degrade')
            self.assertTrue(lock.acquire(timeout=0))
        try:
            self.assertFalse(TaskLock('t_degrade').acquire(timeout=0))
        finally:
            lock.release()

    def test_context_manager(self):
        with task_lock.task_lock('t_ctx', timeout=0) as ok:
            self.assertTrue(ok)
            with task_lock.task_lock('t_ctx', timeout=0) as ok2:
                self.assertFalse(ok2)
        with task_lock.task_lock('t_ctx', timeout=0) as ok3:
            self.assertTrue(ok3)


class FetchTaskMutexTests(_TmpRunDirMixin, SimpleTestCase):
    """REL-04：手动拉取取同一把 stock_quotes 锁；忙时新增关注排队并补拉。"""

    def setUp(self):
        super().setUp()
        with tasks._lock:
            self._orig = dict(tasks._state)
            self._orig_started = tasks._started_at
            tasks._pending.clear()
            tasks._state.update(running=False, task=None, run_id=None, stale=False)

    def tearDown(self):
        with tasks._lock:
            tasks._state.clear()
            tasks._state.update(self._orig)
            tasks._started_at = self._orig_started
            tasks._pending.clear()
        super().tearDown()

    def test_busy_when_lock_held_elsewhere(self):
        holder = TaskLock(tasks.FETCH_LOCK_NAME)
        self.assertTrue(holder.acquire(timeout=0))
        try:
            with mock.patch('stocks.tasks.threading.Thread') as th:
                started, st = tasks.start_fetch_all()
            self.assertFalse(started)
            self.assertTrue(st['locked_by_other'])
            th.assert_not_called()
        finally:
            holder.release()

    def test_request_thread_hands_lock_to_worker(self):
        gate = threading.Event()
        seen = {}

        def fake_all(run_id, light):
            seen['locked'] = TaskLock(tasks.FETCH_LOCK_NAME).locked()
            gate.wait(5)
            return [{'code': 'x'}]

        with mock.patch.object(tasks, '_do_all', fake_all):
            started, st = tasks.start_fetch_all()
            self.assertTrue(started)
            # 忙时新增关注：排队而不是丢弃
            started2, st2 = tasks.start_fetch_one(42, queue_if_busy=True)
            self.assertFalse(started2)
            self.assertTrue(st2['queued'])
            self.assertIn(42, st2['pending'])
            with mock.patch.object(tasks, '_do_one', return_value={'code': '42', 'count': 1}) as one:
                gate.set()
                for _ in range(100):
                    if not tasks.get_fetch_status()['running']:
                        break
                    time.sleep(0.05)
                one.assert_called_once()
                self.assertEqual(one.call_args.args[1], 42)
        self.assertTrue(seen['locked'])
        st = tasks.get_fetch_status()
        self.assertEqual(st['last_status'], 'success')
        self.assertEqual(st['pending'], [])
        self.assertFalse(TaskLock(tasks.FETCH_LOCK_NAME).locked())  # 后台线程结束即释放

    def test_add_stock_queued_when_other_process_holds_lock(self):
        holder = TaskLock(tasks.FETCH_LOCK_NAME)
        self.assertTrue(holder.acquire(timeout=0))
        try:
            with mock.patch.object(tasks, '_do_one', return_value={'code': '1', 'count': 0}) as one:
                started, st = tasks.start_fetch_one(1, queue_if_busy=True)
                self.assertTrue(started)
                self.assertTrue(st['queued'])
                time.sleep(0.3)
                one.assert_not_called()  # 等锁中
                holder.release()
                holder = None
                for _ in range(100):
                    if not tasks.get_fetch_status()['running']:
                        break
                    time.sleep(0.05)
                one.assert_called_once()
        finally:
            if holder is not None:
                holder.release()

    def test_stale_worker_cannot_overwrite_new_run_state(self):
        with tasks._lock:
            run_id = tasks._claim_locked('one')
            tasks._state['run_id'] = run_id + 100  # 模拟已是另一轮
        tasks._finish(run_id, last_status='error', last_error='old')
        self.assertTrue(tasks.get_fetch_status()['running'])


class AddStockFetchResponseTests(TestCase):
    """REL-04：新增关注的首拉结果回给前端，忙时明确告知已排队。"""

    def test_create_reports_queued_when_busy(self):
        with mock.patch('stocks.views.watchlist.start_fetch_one', return_value=(False, {'queued': True})) as m:
            r = self.client.post('/api/stocks/', {'code': '600000', 'name': 'x'}, content_type='application/json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()['fetch']['status'], 'queued')
        self.assertTrue(m.call_args.kwargs['queue_if_busy'])

    def test_create_reports_started_and_restore_path(self):
        with mock.patch('stocks.views.watchlist.start_fetch_one', return_value=(True, {})):
            r = self.client.post('/api/stocks/', {'code': '600001'}, content_type='application/json')
            self.assertEqual(r.json()['fetch']['status'], 'started')
            self.client.delete(f"/api/stocks/{r.json()['id']}/")
            r2 = self.client.post('/api/stocks/', {'code': '600001'}, content_type='application/json')
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(r2.json()['fetch']['status'], 'started')


class GuardedJobTests(_TmpRunDirMixin, SimpleTestCase):
    """REL-04：调度 job 经 _guarded 取锁；盘中忙则跳过，日度任务限时等待。"""

    def test_registered_jobs_are_guarded(self):
        from stocks import scheduler as sch

        jobs = {j.id: j for j in sch.build_scheduler().get_jobs()}
        self.assertEqual(jobs['intraday_fetch'].func.lock_name, 'stock_quotes')
        self.assertEqual(jobs['intraday_fetch'].func.lock_wait, 0)
        self.assertEqual(jobs['daily_summary'].func.lock_name, 'stock_quotes')
        self.assertGreater(jobs['daily_summary'].func.lock_wait, 0)
        self.assertEqual(jobs['preopen_incremental'].func.lock_name, 'stock_quotes')
        self.assertEqual(jobs['detector_scan'].func.lock_name, 'detector_scan')
        self.assertEqual(jobs['post_close_lagging'].func.lock_name, 'market_snapshots')

    def test_guarded_skips_when_busy_and_runs_when_free(self):
        from stocks import scheduler as sch

        calls = []

        def job():
            calls.append(TaskLock('t_job').locked())
            return 'done'

        wrapped = sch._guarded(job, 't_job', 0)
        holder = TaskLock('t_job')
        self.assertTrue(holder.acquire(timeout=0))
        try:
            with self.assertLogs('stocks.scheduler', level='WARNING'):
                self.assertIsNone(wrapped())
        finally:
            holder.release()
        self.assertEqual(calls, [])
        self.assertEqual(wrapped(), 'done')
        self.assertEqual(calls, [True])
        self.assertFalse(TaskLock('t_job').locked())

    def test_commands_take_same_lock(self):
        from io import StringIO

        from django.core.management import CommandError, call_command

        err = StringIO()
        with mock.patch('stocks.detector.scan.run_scan') as rs, \
                mock.patch('stocks.task_lock.TaskLock.acquire', return_value=False) as acq:
            call_command('run_detector_scan', stderr=err)
        rs.assert_not_called()
        self.assertIn('任务锁被占用', err.getvalue())
        self.assertEqual(acq.call_count, 1)

        out = StringIO()
        with mock.patch('stocks.management.commands.fetch_stock_data.task_lock') as tl, \
                mock.patch('stocks.management.commands.fetch_stock_data.fetch_stock_all_data') as fa, \
                self.assertRaises(CommandError):
            tl.return_value.__enter__.return_value = False
            # 锁被占用视为跳过，以非零退出码结束（REL-09）
            call_command('fetch_stock_data', '--all', stdout=out)
        tl.assert_called_once()
        self.assertEqual(tl.call_args.args[0], 'stock_quotes')
        fa.assert_not_called()
        self.assertIn('任务锁被占用', out.getvalue())


class SchedulerStartWhitelistTests(_TmpRunDirMixin, SimpleTestCase):
    """REL-03：Web 内启动改白名单；调度锁被占用时后台定期重试。"""

    def _should(self, argv, env):
        from stocks import scheduler as sch

        base = {'STOCKTRACE_SCHEDULER': '1', 'STOCKTRACE_RUN_SCHEDULER_IN_WEB': '1', 'RUN_MAIN': ''}
        base.update(env)
        with mock.patch.dict(os.environ, base), mock.patch.object(sch.sys, 'argv', argv):
            return sch._should_start_scheduler()

    def test_whitelist(self):
        self.assertTrue(self._should(['/opt/venv/bin/gunicorn', 'StockTrace.wsgi'], {}))
        self.assertTrue(self._should(['/usr/lib/python3/site-packages/gunicorn/__main__.py'], {}))
        self.assertTrue(self._should(['manage.py', 'runserver'], {'RUN_MAIN': 'true'}))
        self.assertFalse(self._should(['manage.py', 'runserver'], {}))  # reloader 父进程
        for cmd in ('fetch_stock_data', 'backfill_market_snapshots', 'validate_market_sources',
                    'run_detector_scan', 'run_scheduler', 'shell', 'migrate'):
            self.assertFalse(self._should(['manage.py', cmd], {'RUN_MAIN': 'true'}), cmd)
        self.assertFalse(self._should(['/opt/venv/bin/gunicorn'], {'STOCKTRACE_RUN_SCHEDULER_IN_WEB': '0'}))
        self.assertFalse(self._should(['/opt/venv/bin/gunicorn'], {'STOCKTRACE_SCHEDULER': '0'}))

    def test_retries_lock_in_background_then_starts(self):
        from stocks import scheduler as sch

        self.assertIsNone(sch.scheduler)
        hb = override_settings(
            STOCKTRACE_SCHEDULER_LOCK=self.run_dir / 'scheduler.lock',
            STOCKTRACE_SCHEDULER_HEARTBEAT=self.run_dir / 'hb.json',
        )
        hb.enable()
        self.addCleanup(hb.disable)
        results = iter([False, False, True])
        fake_sched = mock.MagicMock()
        with mock.patch.object(sch, '_should_start_scheduler', return_value=True), \
                mock.patch.object(sch, '_acquire_lock', side_effect=lambda: next(results)) as acq, \
                mock.patch.object(sch, '_release_lock'), \
                mock.patch.object(sch, 'build_scheduler', return_value=fake_sched), \
                mock.patch.object(sch, 'attach_heartbeat', side_effect=lambda s, mode: s), \
                mock.patch.object(sch, 'write_heartbeat'), \
                mock.patch.object(sch.atexit, 'register'), \
                self.assertLogs('stocks.scheduler', level='WARNING'):
            sch.start(retry_seconds=0.05)
            self.assertIsNone(sch.scheduler)  # 首次没拿到锁，不阻塞请求/启动
            sch.start(retry_seconds=0.05)  # 重试线程在跑时不重复起线程
            for _ in range(100):
                if sch.scheduler is not None:
                    break
                time.sleep(0.02)
            try:
                self.assertIs(sch.scheduler, fake_sched)
                fake_sched.start.assert_called_once()
                self.assertEqual(acq.call_count, 3)
            finally:
                sch.stop()
        self.assertIsNone(sch.scheduler)


class MinuteBarCleanupTests(TestCase):
    """PERF-02：分钟 K 线按保留天数分批清理，并注册为每日 02:30 定时任务。"""

    def _bars(self, stock, start, n):
        from datetime import timedelta

        from .models import MinuteBar

        MinuteBar.objects.bulk_create([
            MinuteBar(stock=stock, datetime=start + timedelta(minutes=i),
                      open=1, close=1, high=1, low=1, volume=1)
            for i in range(n)
        ])

    def test_deletes_only_expired_in_batches(self):
        from datetime import timedelta

        from django.utils import timezone

        from stocks import scheduler as sch

        from .models import MinuteBar, Stock

        stock = Stock.objects.create(code='600000', name='x')
        now = timezone.now()
        self._bars(stock, now - timedelta(days=200), sch.MINUTE_BAR_DELETE_BATCH + 7)  # 跨两批
        self._bars(stock, now - timedelta(days=10), 5)
        with override_settings(MINUTE_BAR_RETENTION_DAYS=120):
            deleted = sch._minute_bar_cleanup_job()
        self.assertEqual(deleted, sch.MINUTE_BAR_DELETE_BATCH + 7)
        self.assertEqual(MinuteBar.objects.count(), 5)
        with override_settings(MINUTE_BAR_RETENTION_DAYS=5):
            self.assertEqual(sch._minute_bar_cleanup_job(), 5)

    def test_job_registered(self):
        from zoneinfo import ZoneInfo

        from stocks import scheduler as sch

        job = {j.id: j for j in sch.build_scheduler().get_jobs()}['minute_bar_cleanup']
        self.assertTrue(job.coalesce)
        self.assertGreaterEqual(job.misfire_grace_time, 600)
        self.assertEqual(job.trigger.timezone, ZoneInfo('Asia/Shanghai'))
        fields = {f.name: str(f) for f in job.trigger.fields}
        self.assertEqual(fields['hour'], '2')
        self.assertEqual(fields['minute'], '30')
        self.assertEqual(job.func.lock_name, 'minute_bar_cleanup')


class SqliteConcurrencyTests(SimpleTestCase):
    """PERF-03：timeout 可配；IMMEDIATE 事务下两连接并发"先读后写"不报 database is locked。"""

    def test_options(self):
        from django.conf import settings

        opts = settings.DATABASES['default']['OPTIONS']
        self.assertEqual(opts['transaction_mode'], 'IMMEDIATE')
        self.assertEqual(opts['timeout'], int(os.environ.get('STOCKTRACE_DB_TIMEOUT', '20')))
        self.assertIn('WAL', opts['init_command'].upper())

    def test_concurrent_read_then_write(self):
        import sqlite3
        from copy import deepcopy

        from django.conf import settings
        from django.db.backends.sqlite3.base import DatabaseWrapper

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / 'perf03.sqlite3'
        init = sqlite3.connect(path)
        init.execute('PRAGMA journal_mode=WAL')
        init.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER)')
        init.commit()
        init.close()

        cfg = deepcopy(settings.DATABASES['default'])
        cfg.update(NAME=str(path), TEST={}, ATOMIC_REQUESTS=False, AUTOCOMMIT=True, CONN_MAX_AGE=0,
                   CONN_HEALTH_CHECKS=False, TIME_ZONE=None, USER='', PASSWORD='', HOST='', PORT='')
        errors, barrier = [], threading.Barrier(2)

        def worker(i):
            w = DatabaseWrapper(deepcopy(cfg), alias=f'perf03_{i}')
            try:
                barrier.wait(5)
                # 与 transaction.atomic 相同的开启方式：BEGIN <transaction_mode>
                w.set_autocommit(False, force_begin_transaction_with_broken_autocommit=True)
                with w.cursor() as c:
                    c.execute('SELECT COUNT(*) FROM t')
                    n = c.fetchone()[0]
                    time.sleep(0.3)  # 让两个事务的"读"重叠
                    c.execute('INSERT INTO t (n) VALUES (%s)', [n])
                w.commit()
                w.set_autocommit(True)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
            finally:
                w.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        check = sqlite3.connect(path)
        self.addCleanup(check.close)
        # IMMEDIATE 串行化：第二个事务读到第一个提交后的计数
        self.assertEqual(sorted(r[0] for r in check.execute('SELECT n FROM t')), [0, 1])


class DashboardAndDailyQueryTests(TestCase):
    """PERF-05：Dashboard 用子查询取每只最新日线；/daily/ 无 N+1、缺省 365 天、上限 3650。"""

    def setUp(self):
        from datetime import timedelta
        from decimal import Decimal

        from django.utils import timezone

        from .models import DailyQuote, Stock

        self.today = timezone.now().date()
        self.a = Stock.objects.create(code='600000', name='A')
        self.b = Stock.objects.create(code='600001', name='B')
        Stock.objects.create(code='600002', name='C')  # 无行情
        for stock, offsets in ((self.a, (0, 1, 2, 400, 4000)), (self.b, (3, 10))):
            for off in offsets:
                DailyQuote.objects.create(
                    stock=stock, trade_date=self.today - timedelta(days=off),
                    open_price=Decimal('10'), close_price=Decimal(10 + off % 7),
                    high_price=Decimal('20'), low_price=Decimal('5'),
                    open_close_diff=Decimal('0'), open_close_pct=Decimal('0'),
                    high_low_diff=Decimal('15'), high_low_pct=Decimal('300'), volume=1,
                )

    def test_dashboard_latest_per_stock_in_two_queries(self):
        from datetime import timedelta

        with self.assertNumQueries(2):
            r = self.client.get('/api/dashboard/')
        self.assertEqual(r.status_code, 200)
        rows = {row['code']: row for row in r.json()}
        self.assertEqual(rows['600000']['trade_date'], self.today.isoformat())
        self.assertEqual(rows['600001']['trade_date'], (self.today - timedelta(days=3)).isoformat())
        self.assertIsNone(rows['600002']['trade_date'])

    def test_daily_default_window_select_related_and_cap(self):
        url = f'/api/stocks/{self.a.id}/daily/'
        with self.assertNumQueries(2):  # get_object + 日线（含 stock join）
            r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()), 3)  # 400 / 4000 天前的不在缺省窗口
        self.assertTrue(all(row['code'] == '600000' for row in r.json()))
        self.assertEqual(len(self.client.get(url, {'days': 500}).json()), 4)
        self.assertEqual(len(self.client.get(url, {'days': 99999}).json()), 4)  # 上限 3650 天


class MarketDeadlineBreakerTests(SimpleTestCase):
    """REL-05：市场接口总时限超时 504；同接口连续 3 次超时熔断 60s 返回 503。"""

    def setUp(self):
        from stocks import views
        from stocks.views import market as market_views

        self.views = market_views  # patch 目标须是定义模块（ARC-01 拆包）
        self.breaker = views._breaker
        views._breaker.clear()
        self.addCleanup(views._breaker.clear)
        self.release = threading.Event()
        self.addCleanup(self.release.set)  # 放走仍卡在池里的慢 loader

    def _slow(self, *a, **k):
        self.release.wait(10)
        return {'ok': True}

    def _get(self, url):
        from rest_framework.test import APIRequestFactory

        from stocks import views

        req = APIRequestFactory().get(url)
        return views.market_overview(req) if url == '/api/market/' else views.market_sectors(req)

    def test_default_deadline_below_nginx(self):
        from django.conf import settings

        self.assertLess(settings.STOCKTRACE_REQUEST_DEADLINE, 120)

    @override_settings(STOCKTRACE_REQUEST_DEADLINE=0.2)
    def test_timeout_returns_504_and_breaker_opens(self):
        # 熔断日志随 _call_with_deadline 迁到 stocks.common.upstream，取父 logger 一并捕获
        with mock.patch.object(self.views, 'get_market_overview', side_effect=self._slow) as m, \
                self.assertLogs('stocks', level='WARNING') as cm:
            for _ in range(3):
                t0 = time.monotonic()
                r = self._get('/api/market/')
                self.assertEqual(r.status_code, 504)
                self.assertLess(time.monotonic() - t0, 2)
            r = self._get('/api/market/')
            self.assertEqual(r.status_code, 503)
            self.assertEqual(m.call_count, 3)  # 熔断期间不再打上游
        self.assertTrue(any('熔断' in line for line in cm.output))
        # 熔断按接口隔离：其他接口不受影响
        with mock.patch.object(self.views, 'get_sector_rotation', return_value={'items': []}):
            self.assertEqual(self._get('/api/market/sectors/').status_code, 200)

    @override_settings(STOCKTRACE_REQUEST_DEADLINE=0.2)
    def test_success_resets_consecutive_count_and_breaker_expires(self):
        with mock.patch.object(self.views, 'get_market_overview', side_effect=self._slow), \
                self.assertLogs('stocks.views', level='WARNING'):
            self._get('/api/market/')
            self._get('/api/market/')
        with mock.patch.object(self.views, 'get_market_overview', return_value={'ok': 1}):
            self.assertEqual(self._get('/api/market/').status_code, 200)
        self.assertNotIn('大盘数据', self.breaker)
        # 熔断到期后恢复访问
        self.breaker['大盘数据'] = {'timeouts': 0, 'open_until': time.monotonic() - 1}
        with mock.patch.object(self.views, 'get_market_overview', return_value={'ok': 1}):
            self.assertEqual(self._get('/api/market/').status_code, 200)

    def test_errors_still_map_to_400_and_502(self):
        from stocks.errors import BadRequest

        # ARC-05：只有 BadRequest 是 400；上游解析抛的普通 ValueError 改走 502（见下方用例）
        with mock.patch.object(self.views, 'get_sector_rotation', side_effect=BadRequest('bad')):
            self.assertEqual(self._get('/api/market/sectors/').status_code, 400)
        with mock.patch.object(self.views, 'get_sector_rotation', side_effect=RuntimeError('x')), \
                self.assertLogs('stocks.views', level='ERROR'):
            self.assertEqual(self._get('/api/market/sectors/').status_code, 502)

    def test_frontend_timeout_below_nginx(self):
        js = (Path(__file__).resolve().parents[2] / 'frontend' / 'src' / 'api' / 'stocks.js').read_text(encoding='utf-8')
        self.assertNotIn('180000', js)
        # 选项对象里还可能有 params / signal 等，只要求该接口显式用 110s 超时
        self.assertRegex(
            js, r"api\.get\('/market/national-etf/flow/',\s*\{[^}]*\btimeout:\s*110000\b[^}]*\}\)",
        )
