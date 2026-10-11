"""P1 调度可靠性与性能修复的回归测试（REL-03 / REL-04 / REL-05 / PERF-02 / PERF-03 / PERF-05）。"""

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
        with mock.patch('stocks.views.start_fetch_one', return_value=(False, {'queued': True})) as m:
            r = self.client.post('/api/stocks/', {'code': '600000', 'name': 'x'}, content_type='application/json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()['fetch']['status'], 'queued')
        self.assertTrue(m.call_args.kwargs['queue_if_busy'])

    def test_create_reports_started_and_restore_path(self):
        with mock.patch('stocks.views.start_fetch_one', return_value=(True, {})):
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

        from django.core.management import call_command

        err = StringIO()
        with mock.patch('stocks.detector.scan.run_scan') as rs, \
                mock.patch('stocks.task_lock.TaskLock.acquire', return_value=False) as acq:
            call_command('run_detector_scan', stderr=err)
        rs.assert_not_called()
        self.assertIn('任务锁被占用', err.getvalue())
        self.assertEqual(acq.call_count, 1)

        out = StringIO()
        with mock.patch('stocks.management.commands.fetch_stock_data.task_lock') as tl, \
                mock.patch('stocks.management.commands.fetch_stock_data.fetch_all_active_stocks') as fa:
            tl.return_value.__enter__.return_value = False
            call_command('fetch_stock_data', '--all', stdout=out)
        tl.assert_called_once()
        self.assertEqual(tl.call_args.args[0], 'stock_quotes')
        fa.assert_not_called()
        self.assertIn('任务锁被占用', out.getvalue())
