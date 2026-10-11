"""OPS-08 覆盖率补齐：调度任务守卫、清理任务、文件锁与 Web 内兼容启动。

上游与重活（services/market/detector 入口）一律 mock，只验证调度层自己的判断。
"""

import os
import shutil
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from . import scheduler as sch
from .models import AiCallLog, MarketDailySnapshot, MinuteBar, Stock

_TZ = timezone.get_default_timezone()


def _at(h, m=0):
    return timezone.make_aware(datetime(2026, 10, 12, h, m), _TZ)


class JobGuardTests(SimpleTestCase):
    """调度守卫：非交易日 / 日历未知 / 时间窗外一律不触发重活。"""

    def test_preopen_skips_unless_trading_day(self):
        for flag in (False, None):
            with patch('stocks.trading_calendar.is_trading_day', return_value=flag), \
                    patch('stocks.services.fetch_incremental_daily_all') as inc:
                sch._preopen_incremental_job()
            inc.assert_not_called()
        with patch('stocks.trading_calendar.is_trading_day', return_value=True), \
                patch('stocks.services.fetch_incremental_daily_all') as inc:
            sch._preopen_incremental_job()
        inc.assert_called_once()

    def test_post_close_window_inclusive_bounds(self):
        cases = [((15, 0), False), ((15, 29), False), ((15, 30), True),
                 ((21, 0), True), ((21, 1), False)]
        for (h, m), expected in cases:
            with self.subTest(t=f'{h}:{m}'), \
                    patch('stocks.trading_calendar.is_trading_day', return_value=True), \
                    patch('django.utils.timezone.localtime', return_value=_at(h, m)), \
                    patch('stocks.market.warm_post_close_lagging') as warm:
                sch._post_close_lagging_job()
            self.assertEqual(warm.called, expected)
        with patch('stocks.trading_calendar.is_trading_day', return_value=None), \
                patch('stocks.market.warm_post_close_lagging') as warm:
            sch._post_close_lagging_job()
        warm.assert_not_called()

    def test_detector_scan_guards(self):
        with patch.dict(os.environ, {'STOCKTRACE_DETECTOR': 'off'}), \
                patch('stocks.detector.scan.run_scan') as scan:
            sch._detector_scan_job()
        scan.assert_not_called()
        with patch.dict(os.environ, {'STOCKTRACE_DETECTOR': '1'}):
            with patch('stocks.trading_calendar.is_trading_day', return_value=False), \
                    patch('stocks.detector.scan.run_scan') as scan:
                sch._detector_scan_job()
            scan.assert_not_called()
            with patch('stocks.trading_calendar.is_trading_day', return_value=True), \
                    patch('django.utils.timezone.localtime', return_value=_at(15, 29)), \
                    patch('stocks.detector.scan.run_scan') as scan:
                sch._detector_scan_job()
            scan.assert_not_called()
            with patch('stocks.trading_calendar.is_trading_day', return_value=True), \
                    patch('django.utils.timezone.localtime', return_value=_at(17, 15)), \
                    patch('stocks.detector.scan.run_scan', side_effect=RuntimeError('x')) as scan, \
                    self.assertLogs('stocks.scheduler', level='ERROR'):
                sch._detector_scan_job()  # 失败只记日志
            scan.assert_called_once()

    def test_intraday_and_daily_jobs_chain(self):
        with patch('stocks.services.fetch_intraday_update') as intraday, \
                patch('stocks.alerts.evaluate_alerts') as alerts:
            sch._intraday_job()
        intraday.assert_called_once()
        alerts.assert_called_once()
        with patch('stocks.services.fetch_daily_summary') as summary, \
                patch('stocks.alerts.evaluate_alerts', side_effect=RuntimeError('x')), \
                patch.object(sch, '_cleanup_ai_logs') as ai, \
                patch.object(sch, '_cleanup_market_snapshots') as snaps, \
                self.assertLogs('stocks.scheduler', level='ERROR'):
            sch._daily_summary_job()  # 提醒评估失败不影响后续清理
        summary.assert_called_once()
        ai.assert_called_once()
        snaps.assert_called_once()


class GuardedWrapperTests(SimpleTestCase):
    def test_lock_busy_skips_and_releases_on_success(self):
        lock = MagicMock()
        lock.acquire.return_value = False
        func = MagicMock(__name__='job')
        with patch('stocks.task_lock.TaskLock', return_value=lock), \
                self.assertLogs('stocks.scheduler', level='WARNING'):
            self.assertIsNone(sch._guarded(func, 'x', 5)())
        func.assert_not_called()
        lock.acquire.return_value = True
        func.return_value = 42
        with patch('stocks.task_lock.TaskLock', return_value=lock):
            wrapped = sch._guarded(func, 'x', 5)
            self.assertEqual(wrapped(), 42)
        lock.release.assert_called_once()
        self.assertEqual((wrapped.lock_name, wrapped.lock_wait), ('x', 5))


class CleanupJobTests(TestCase):
    def test_ai_logs_retention(self):
        old = AiCallLog.objects.create(purpose='analysis')
        AiCallLog.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=91))
        AiCallLog.objects.create(purpose='analysis')
        with override_settings(AI_CALL_LOG_RETENTION_DAYS=90):
            sch._cleanup_ai_logs()
        self.assertEqual(AiCallLog.objects.count(), 1)

    def test_snapshot_retention(self):
        today = timezone.localdate()
        MarketDailySnapshot.objects.create(kind='industry_ff', trade_date=today - timedelta(days=601), payload=[])
        MarketDailySnapshot.objects.create(kind='industry_ff', trade_date=today - timedelta(days=10), payload=[])
        with override_settings(MARKET_SNAPSHOT_RETENTION_DAYS=600):
            sch._cleanup_market_snapshots()
        self.assertEqual(MarketDailySnapshot.objects.count(), 1)

    def test_cleanup_errors_are_logged_not_raised(self):
        with patch('stocks.models.AiCallLog.objects') as qs, \
                self.assertLogs('stocks.scheduler', level='ERROR'):
            qs.filter.side_effect = RuntimeError('db')
            sch._cleanup_ai_logs()
        with patch('stocks.models.MarketDailySnapshot.objects') as qs, \
                self.assertLogs('stocks.scheduler', level='ERROR'):
            qs.filter.side_effect = RuntimeError('db')
            sch._cleanup_market_snapshots()

    def test_minute_bar_cleanup_batches(self):
        stock = Stock.objects.create(code='600000', name='浦发银行')
        old = timezone.now() - timedelta(days=200)
        MinuteBar.objects.bulk_create([
            MinuteBar(stock=stock, datetime=old + timedelta(minutes=i),
                      open=1, close=1, high=1, low=1, volume=1)
            for i in range(5)
        ])
        MinuteBar.objects.create(stock=stock, datetime=timezone.now(), open=1, close=1,
                                 high=1, low=1, volume=1)
        with patch.object(sch, 'MINUTE_BAR_DELETE_BATCH', 2), \
                override_settings(MINUTE_BAR_RETENTION_DAYS=120):
            self.assertEqual(sch._minute_bar_cleanup_job(), 5)
        self.assertEqual(MinuteBar.objects.count(), 1)


class FileLockTests(SimpleTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_second_holder_is_rejected_until_release(self):
        lock = self.tmp / 'scheduler.lock'
        with self.settings(STOCKTRACE_SCHEDULER_LOCK=lock), patch.object(sch, '_lock_fh', None):
            self.assertTrue(sch._acquire_lock())
            first = sch._lock_fh
            sch._lock_fh = None
            try:
                self.assertFalse(sch._acquire_lock())  # 另一个打开的句柄拿不到排他锁
                self.assertIsNone(sch._lock_fh)
            finally:
                sch._lock_fh = first
                sch._release_lock()
            self.assertIsNone(sch._lock_fh)
            self.assertTrue(sch._acquire_lock())
            sch._release_lock()
            sch._release_lock()  # 未持有时再释放是空操作

    def test_unwritable_lock_path_returns_false(self):
        blocker = self.tmp / 'file'
        blocker.write_text('x')
        with self.settings(STOCKTRACE_SCHEDULER_LOCK=blocker / 'sub' / 'scheduler.lock'), \
                patch.object(sch, '_lock_fh', None), \
                self.assertLogs('stocks.scheduler', level='WARNING'):
            self.assertFalse(sch._acquire_lock())


class WebStartTests(SimpleTestCase):
    """REL-03：Web 内兼容启动——白名单判定、拿锁即启动、锁被占时后台重试、stop 收尾。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        ctx = self.settings(STOCKTRACE_SCHEDULER_HEARTBEAT=self.tmp / 'hb.json',
                            STOCKTRACE_SCHEDULER_LOCK=self.tmp / 'scheduler.lock')
        ctx.enable()
        self.addCleanup(ctx.disable)
        for name in ('scheduler', '_retry_thread', '_lock_fh'):
            patcher = patch.object(sch, name, None)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(sch, '_atexit_registered', True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(sch._retry_stop.clear)

    def test_whitelist(self):
        env = {'STOCKTRACE_SCHEDULER': '1', 'STOCKTRACE_RUN_SCHEDULER_IN_WEB': '1'}
        with patch.dict(os.environ, env):
            with patch.object(sch.sys, 'argv', ['/venv/bin/gunicorn', 'StockTrace.wsgi']):
                self.assertTrue(sch._should_start_scheduler())
            with patch.object(sch.sys, 'argv', ['manage.py', 'runserver']), \
                    patch.dict(os.environ, {'RUN_MAIN': 'true'}):
                self.assertTrue(sch._should_start_scheduler())
            with patch.object(sch.sys, 'argv', ['manage.py', 'runserver']), \
                    patch.dict(os.environ, {'RUN_MAIN': ''}):
                self.assertFalse(sch._should_start_scheduler())  # reloader 父进程
        with patch.dict(os.environ, {**env, 'STOCKTRACE_SCHEDULER': 'off'}), \
                patch.object(sch.sys, 'argv', ['gunicorn']):
            self.assertFalse(sch._should_start_scheduler())

    def test_start_runs_scheduler_and_stop_releases(self):
        with patch.object(sch, '_should_start_scheduler', return_value=True):
            sch.start()
            try:
                self.assertIsNotNone(sch.scheduler)
                self.assertTrue(sch.scheduler.running)
                self.assertEqual(sch.read_heartbeat()['mode'], 'web')
                sch.start()  # 已启动：幂等
            finally:
                sch.stop()
        self.assertIsNone(sch.scheduler)
        self.assertIsNone(sch._lock_fh)
        self.assertFalse(sch.read_heartbeat()['running'])

    def test_lock_held_starts_retry_thread_until_stop(self):
        with patch.object(sch, '_should_start_scheduler', return_value=True), \
                patch.object(sch, '_acquire_lock', return_value=False), \
                self.assertLogs('stocks.scheduler', level='WARNING'):
            sch.start(retry_seconds=0.05)
            thread = sch._retry_thread
            self.assertTrue(thread.is_alive())
            sch.stop()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(sch.scheduler)

    def test_retry_loop_starts_once_lock_free(self):
        started = threading.Event()
        with patch.object(sch, '_acquire_lock', side_effect=[False, True]), \
                patch.object(sch, '_start_web_scheduler_locked', side_effect=started.set), \
                self.assertLogs('stocks.scheduler', level='WARNING'):
            sch._retry_lock_loop(0.01)
        self.assertTrue(started.is_set())

    def test_not_whitelisted_is_noop(self):
        with patch.object(sch, '_should_start_scheduler', return_value=False), \
                patch.object(sch, '_acquire_lock') as acq:
            sch.start()
        acq.assert_not_called()
