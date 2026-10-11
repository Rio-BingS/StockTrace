"""
APScheduler 集成到 Django

在 Django 启动时自动注册定时任务：
1. 开盘前 08:50 增量日线（预热，避免手动时补大段）
2. 盘中每5分钟更新分钟数据（随后评估价格提醒）
3. 收盘后 15:10 汇总当日日线+分钟（自选库最后一次自动写入；随后评估价格提醒、清理 AI 调用记录）
4. 收盘后 15:30–21:00 每30分钟预热晚到行情缓存并落日度快照（交易日）
5. 每交易日 17:15 探测器扫描（板块/大盘快照已在前两轮预热落库后）
6. 每日 02:30 清理超出保留期的分钟 K 线

运行方式（REL-01）：
- 生产：独立进程 `python manage.py run_scheduler`（systemd 单元 stocktrace-scheduler），
  不再寄生在 Gunicorn worker 里——worker 被 max_requests 回收/重启时会杀掉长任务、错过定时点。
- 兼容：设置 STOCKTRACE_RUN_SCHEDULER_IN_WEB=1 时仍由 AppConfig.ready() 在 Web 进程内启动
  （白名单：仅 gunicorn 或 runserver 子进程；锁被占用时后台每 60s 重试）。
两种方式都用同一把文件锁（STOCKTRACE_SCHEDULER_LOCK，默认 <STOCKTRACE_RUN_DIR>/scheduler.lock）
保证全机只有一个进程持有调度器。
"""

import atexit
import functools
import json
import logging
import os
import sys
import threading
from datetime import time as dt_time

from zoneinfo import ZoneInfo

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

scheduler = None
_lock_fh = None

# Web 内兼容模式下，调度锁被占用时的后台重试间隔（秒）（REL-03）
LOCK_RETRY_SECONDS = 60
_start_mutex = threading.Lock()
_retry_stop = threading.Event()
_retry_thread = None
_atexit_registered = False

# A 股交易时间天然是北京时间：trigger 显式带时区，不依赖服务器/进程的本地时区
SCHEDULER_TZ = ZoneInfo('Asia/Shanghai')

# 任务默认：错过时间点后在宽限期内仍补跑（APScheduler 3.x 默认只有 1s，进程稍忙/重启就整次跳过）；
# coalesce 把积压的多次合并成一次；max_instances=1 防止同一任务重叠执行
JOB_DEFAULTS = {
    'misfire_grace_time': 900,
    'coalesce': True,
    'max_instances': 1,
}

# 收盘后降频窗口（含端点）；用于挡住 cron 在 15:00 的误触发
_POST_CLOSE_START = dt_time(15, 30)
_POST_CLOSE_END = dt_time(21, 0)


def _is_web_server_process():
    """当前进程是否为 Web 服务进程：gunicorn（含 python -m gunicorn），或 runserver 的
    reloader 子进程（只有子进程 RUN_MAIN=true）。管理命令、shell、测试等一律不是。"""
    if sys.argv and 'gunicorn' in sys.argv[0].lower():
        return True
    argv = [a.lower() for a in sys.argv[1:]]
    return 'runserver' in argv and os.environ.get('RUN_MAIN') == 'true'


def _should_start_scheduler():
    """白名单（REL-03）：全局开关未关、STOCKTRACE_RUN_SCHEDULER_IN_WEB=1，且确实是 Web 服务进程。

    旧实现靠 argv 黑名单，漏掉的管理命令（fetch_stock_data、backfill_* 等）会在 CLI 进程里
    启动调度器、抢走调度锁，甚至以 root 身份建出锁文件。
    """
    if os.environ.get('STOCKTRACE_SCHEDULER', '1').lower() in ('0', 'false', 'no', 'off'):
        return False
    from .apps import run_scheduler_in_web

    if not run_scheduler_in_web():
        return False
    return _is_web_server_process()


def _acquire_lock():
    """Cross-platform exclusive lock; returns True if this process owns the scheduler."""
    global _lock_fh
    lock_path = str(getattr(settings, 'STOCKTRACE_SCHEDULER_LOCK', os.path.join(settings.BASE_DIR, 'scheduler.lock')))
    try:
        os.makedirs(os.path.dirname(lock_path) or '.', exist_ok=True)
        _lock_fh = open(lock_path, 'a+')
        if sys.platform == 'win32':
            import msvcrt
            _lock_fh.seek(0)
            try:
                msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                _lock_fh.close()
                _lock_fh = None
                return False
        else:
            import fcntl
            try:
                fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                _lock_fh.close()
                _lock_fh = None
                return False
        _lock_fh.seek(0)
        _lock_fh.truncate()
        _lock_fh.write(str(os.getpid()))
        _lock_fh.flush()
        return True
    except Exception as e:
        logger.warning(f"调度文件锁获取失败: {e}")
        if _lock_fh is not None:
            try:
                _lock_fh.close()
            except Exception:
                pass
            _lock_fh = None
        return False


def _release_lock():
    global _lock_fh
    if _lock_fh is None:
        return
    try:
        if sys.platform == 'win32':
            import msvcrt
            _lock_fh.seek(0)
            try:
                msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        _lock_fh.close()
    except Exception:
        pass
    _lock_fh = None


def _run_steps(job_name, steps):
    """逐步执行任务链（REL-08）：每一步单独 try，前一步异常不跳过后面的提醒与清理。

    steps: [(步骤名, 可调用对象)]。返回失败的步骤名列表；全部失败信息带堆栈写日志。
    """
    failed = []
    for name, step in steps:
        try:
            step()
        except Exception:
            failed.append(name)
            logger.exception(f'定时任务 {job_name} 的步骤「{name}」失败，继续后续步骤')
    if failed:
        logger.error(f'定时任务 {job_name} 完成，失败步骤：{"、".join(failed)}')
    return failed


def _intraday_job():
    from .services import fetch_intraday_update
    return _run_steps('intraday_fetch', [
        ('盘中行情拉取', fetch_intraday_update),
        ('价格提醒评估', _evaluate_alerts),
    ])


def _daily_summary_job():
    from .services import fetch_daily_summary
    return _run_steps('daily_summary', [
        ('收盘日线汇总', fetch_daily_summary),
        ('价格提醒评估', _evaluate_alerts),
        ('AI 调用记录清理', _cleanup_ai_logs),
        ('市场快照清理', _cleanup_market_snapshots),
    ])


def _evaluate_alerts():
    """提醒评估跟在数据任务后跑；失败只记日志，不影响主任务结果。"""
    from .alerts import evaluate_alerts
    try:
        evaluate_alerts()
    except Exception:
        logger.exception("价格提醒评估失败")


def _cleanup_ai_logs():
    """AI 调用流水保留 N 天（默认 90），防止 AiCallLog 无限增长。"""
    from datetime import timedelta

    from .models import AiCallLog

    days = getattr(settings, 'AI_CALL_LOG_RETENTION_DAYS', 90)
    try:
        cutoff = timezone.now() - timedelta(days=days)
        deleted, _ = AiCallLog.objects.filter(created_at__lt=cutoff).delete()
        if deleted:
            logger.info(f"清理 {days} 天前的 AI 调用记录 {deleted} 条")
    except Exception as e:
        logger.error(f"AI 调用记录清理失败: {e}")


def _cleanup_market_snapshots():
    """市场日度快照保留 N 天（默认 600，覆盖 1y 区间所需约 525 自然日），防 SQLite 无限膨胀。"""
    from datetime import timedelta

    from .models import MarketDailySnapshot

    days = settings.MARKET_SNAPSHOT_RETENTION_DAYS  # settings 已接线环境变量并保证下限（OPS-06）
    try:
        cutoff = timezone.localdate() - timedelta(days=days)
        deleted, _ = MarketDailySnapshot.objects.filter(trade_date__lt=cutoff).delete()
        if deleted:
            logger.info(f"清理 {days} 天前的市场日度快照 {deleted} 行")
    except Exception as e:
        logger.error(f"市场日度快照清理失败: {e}")


MINUTE_BAR_DELETE_BATCH = 5000


def _minute_bar_cleanup_job():
    """分钟 K 线保留 N 天（默认 120，MINUTE_BAR_RETENTION_DAYS），分批删除（PERF-02）。

    每批 5000 行一个短事务：SQLite 单写锁，一次性删几十万行会长时间挡住盘中写入。
    按主键取批（旧数据主键小，扫描很快结束），不按默认的 datetime 排序整表。
    """
    from datetime import timedelta

    from .models import MinuteBar

    days = int(getattr(settings, 'MINUTE_BAR_RETENTION_DAYS', 120))
    cutoff = timezone.now() - timedelta(days=days)
    total = 0
    while True:
        batch = (
            MinuteBar.objects.filter(datetime__lt=cutoff)
            .order_by('pk').values_list('pk', flat=True)[:MINUTE_BAR_DELETE_BATCH]
        )
        deleted, _ = MinuteBar.objects.filter(pk__in=list(batch)).delete()
        total += deleted
        if deleted < MINUTE_BAR_DELETE_BATCH:
            break
    if total:
        logger.info(f"清理 {days} 天前的分钟K线 {total} 行")
    return total


def _preopen_incremental_job():
    """开盘前只补日线缺口，压力小。"""
    from .services import fetch_incremental_daily_all
    from .trading_calendar import is_trading_day
    if not is_trading_day():
        logger.debug("非交易日，跳过开盘前增量日线")
        return
    fetch_incremental_daily_all()


def _post_close_lagging_job():
    """
    收盘后降频：仅预热可能晚到的行情缓存（北向/大盘资金/ETF 份额）。
    交易日 + [15:30, 21:00]；21:00 为当天最后一次。
    """
    from .market import warm_post_close_lagging
    from .trading_calendar import is_trading_day

    if not is_trading_day():
        logger.debug("非交易日，跳过收盘后晚到数据预热")
        return

    now = timezone.localtime()
    t = now.time().replace(second=0, microsecond=0)
    if t < _POST_CLOSE_START or t > _POST_CLOSE_END:
        logger.debug("不在收盘后降频窗口 [15:30, 21:00]，跳过")
        return

    warm_post_close_lagging()


# 探测器依赖的当日快照（REL-08）：17:15 首次尝试，缺快照则每 30 分钟重试，
# 最后一次（18:45）不再等待，降级照跑（维度自行标记降级）
_DETECTOR_REQUIRED_SNAPSHOTS = ('industry_ff', 'market_ff')
_DETECTOR_LAST_ATTEMPT = dt_time(18, 45)


def _detector_dependencies_ready(trade_date):
    """返回 (是否应现在扫描, 原因)。已扫过的当日不重复；快照未齐且非最后一次则延后。"""
    from .models import DetectorRun, MarketDailySnapshot

    done = DetectorRun.objects.filter(trade_date=trade_date).exclude(status='failed').exists()
    if done:
        return False, 'already_done'
    have = set(MarketDailySnapshot.objects.filter(
        trade_date=trade_date, kind__in=_DETECTOR_REQUIRED_SNAPSHOTS,
    ).values_list('kind', flat=True))
    missing = [k for k in _DETECTOR_REQUIRED_SNAPSHOTS if k not in have]
    if not missing:
        return True, 'ready'
    if timezone.localtime().time() >= _DETECTOR_LAST_ATTEMPT:
        logger.warning(f'探测器最后一次尝试：当日快照仍缺 {missing}，降级扫描')
        return True, 'degraded'
    logger.warning(f'探测器延后：当日快照未落库 {missing}，30 分钟后重试')
    return False, 'waiting'


def _detector_scan_job():
    """探测器日度扫描：每交易日 17:15 起（快照已落库、两融 T+1 基本可取）。

    为什么 17:15 独立成任务而不是并入 15:30 预热链：板块/大盘快照要等
    15:30/16:00 两轮预热写完，且融资余额快照只应每天探测一次（深市按日
    探测有请求成本）；失败只记日志，不影响其他任务。按依赖编排（REL-08）：
    当日 industry_ff/market_ff 快照未落库则延后 30 分钟重试，最多 3 次。
    """
    if os.environ.get('STOCKTRACE_DETECTOR', '1').lower() in ('0', 'false', 'no', 'off'):
        return
    from .trading_calendar import is_trading_day

    if not is_trading_day():
        logger.debug('非交易日，跳过探测器扫描')
        return
    now = timezone.localtime()
    if now.time() < dt_time(15, 30):
        # 收盘守卫：与快照落库一致，防止 cron 误触发把上一交易日盘面标成今天
        logger.debug('未到 15:30 收盘，跳过探测器扫描')
        return

    ready, _reason = _detector_dependencies_ready(now.date())
    if not ready:
        return

    from .detector.scan import run_scan

    try:
        run_scan()
    except Exception:
        logger.exception('探测器扫描失败')


# 任务互斥（REL-04）：与手动拉取、管理命令共用 task_lock 的同名锁。
# 盘中任务忙则直接跳过（下一轮 5 分钟后再来）；日度任务限时等待，等不到才跳过。
_INTRADAY_SKIP = 0
_DAILY_WAIT_SECONDS = 1800


def _guarded(func, lock_name, wait=_INTRADAY_SKIP):
    """包装定时任务：执行前取任务锁，取不到记 WARNING 并跳过本次。"""
    from .task_lock import TaskLock

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        lock = TaskLock(lock_name)
        if not lock.acquire(timeout=wait):
            logger.warning(f'定时任务 {func.__name__} 跳过：任务锁 {lock_name} 被占用（已等待 {wait}s）')
            return None
        try:
            return func(*args, **kwargs)
        finally:
            lock.release()

    wrapper.lock_name = lock_name
    wrapper.lock_wait = wait
    return wrapper


def register_jobs(sched):
    """注册全部定时任务（Web 内启动与 run_scheduler 共用同一份注册）。

    每个 job 都显式 timezone=Asia/Shanghai、coalesce=True 并给出 misfire_grace_time：
    盘中 5 分钟任务宽限 4 分钟（再晚就该让位给下一轮）；日度任务宽限更长，
    它们自带交易日/收盘时间守卫，晚跑不会写错日期。
    """
    def add(func, trigger, job_id, grace):
        sched.add_job(
            func,
            trigger,
            id=job_id,
            replace_existing=True,
            misfire_grace_time=grace,
            coalesce=True,
            max_instances=1,
        )

    # 开盘前：增量日线预热
    add(_guarded(_preopen_incremental_job, 'stock_quotes', _DAILY_WAIT_SECONDS),
        CronTrigger(hour=8, minute=50, timezone=SCHEDULER_TZ),
        'preopen_incremental', 1800)

    add(_guarded(_intraday_job, 'stock_quotes', _INTRADAY_SKIP),
        CronTrigger(hour='9-14', minute='*/5', timezone=SCHEDULER_TZ),
        'intraday_fetch', 240)

    add(_guarded(_daily_summary_job, 'stock_quotes', _DAILY_WAIT_SECONDS),
        CronTrigger(hour=15, minute=10, timezone=SCHEDULER_TZ),
        'daily_summary', 3600)

    # 收盘后：15:30–21:00 每 30 分钟预热晚到行情（cron 含 15:00，由窗口守卫丢弃）
    add(_guarded(_post_close_lagging_job, 'market_snapshots', 600),
        CronTrigger(day_of_week='mon-fri', hour='15,16,17,18,19,20,21', minute='0,30', timezone=SCHEDULER_TZ),
        'post_close_lagging', 900)

    # 探测器：交易日 17:15（板块/大盘快照已在前两轮预热落库）；
    # 17:45/18:15/18:45 为依赖未齐时的重试点，当日已扫过则直接跳过（REL-08）
    add(_guarded(_detector_scan_job, 'detector_scan', _DAILY_WAIT_SECONDS),
        CronTrigger(day_of_week='mon-fri', hour='17-18', minute='15,45', timezone=SCHEDULER_TZ),
        'detector_scan', 1500)

    # 分钟 K 线保留期清理：每日 02:30 低峰执行（PERF-02）
    add(_guarded(_minute_bar_cleanup_job, 'minute_bar_cleanup', _DAILY_WAIT_SECONDS),
        CronTrigger(hour=2, minute=30, timezone=SCHEDULER_TZ),
        'minute_bar_cleanup', 3600)
    return sched


# ---------------------------------------------------------------------------
# 心跳（REL-02）：调度进程把存活时间、各 job 下次运行时间、最近一次结果写到 JSON 文件，
# Web 进程的 /api/health/ 读取它判断调度是否在跑——两者是不同进程，不能用进程内缓存。
# ---------------------------------------------------------------------------

HEARTBEAT_JOB_ID = 'scheduler_heartbeat'
HEARTBEAT_INTERVAL_SECONDS = 60

_hb_lock = threading.Lock()
_hb_state = {'started_at': None, 'mode': None, 'last_runs': {}}


def heartbeat_path():
    return str(getattr(
        settings, 'STOCKTRACE_SCHEDULER_HEARTBEAT',
        os.path.join(settings.BASE_DIR, 'scheduler_heartbeat.json'),
    ))


def read_heartbeat():
    """读取心跳文件；不存在/损坏返回 None。"""
    try:
        with open(heartbeat_path(), encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _iso(dt):
    return dt.isoformat() if dt is not None else None


def write_heartbeat(sched, running=True):
    """原子写入心跳文件（tmp + os.replace，读方永远看不到半个 JSON）。失败只记日志。"""
    jobs = []
    try:
        for job in sched.get_jobs():
            if job.id == HEARTBEAT_JOB_ID:
                continue
            jobs.append({'id': job.id, 'next_run_time': _iso(getattr(job, 'next_run_time', None))})
    except Exception:
        logger.exception('读取调度任务列表失败')
    with _hb_lock:
        payload = {
            'pid': os.getpid(),
            'mode': _hb_state['mode'],
            'running': bool(running),
            'started_at': _hb_state['started_at'],
            'updated_at': timezone.now().isoformat(),
            'jobs': jobs,
            'last_runs': dict(_hb_state['last_runs']),
        }
        path = heartbeat_path()
        tmp = f'{path}.{os.getpid()}.tmp'
        try:
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f'调度心跳写入失败 {path}: {e}')


def _make_job_listener(sched):
    def _on_job_event(event):
        if event.job_id == HEARTBEAT_JOB_ID:
            return
        if event.code == EVENT_JOB_ERROR:
            status = 'error'
            # APScheduler 自身已在 apscheduler.executors.* 记录带堆栈的 ERROR，这里补一条业务口径
            logger.error(f'定时任务 {event.job_id} 执行失败: {event.exception!r}')
        elif event.code == EVENT_JOB_MISSED:
            status = 'missed'
            logger.warning(f'定时任务 {event.job_id} 错过执行时间 {event.scheduled_run_time}（超出 misfire 宽限）')
        else:
            status = 'ok'
        with _hb_lock:
            _hb_state['last_runs'][event.job_id] = {
                'status': status,
                'scheduled_run_time': _iso(getattr(event, 'scheduled_run_time', None)),
                'finished_at': timezone.now().isoformat(),
                'error': repr(event.exception)[:300] if getattr(event, 'exception', None) else None,
            }
        write_heartbeat(sched)
    return _on_job_event


def attach_heartbeat(sched, mode):
    """给调度器挂上心跳任务与 job 结果监听；沿用旧心跳文件里的最近结果（重启后不丢"最近任务时间"）。"""
    prev = read_heartbeat() or {}
    with _hb_lock:
        _hb_state['started_at'] = timezone.now().isoformat()
        _hb_state['mode'] = mode
        _hb_state['last_runs'] = dict(prev.get('last_runs') or {})
    sched.add_listener(_make_job_listener(sched), EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED)
    sched.add_job(
        write_heartbeat,
        IntervalTrigger(seconds=HEARTBEAT_INTERVAL_SECONDS, timezone=SCHEDULER_TZ),
        args=[sched],
        id=HEARTBEAT_JOB_ID,
        replace_existing=True,
        misfire_grace_time=30,
        coalesce=True,
        max_instances=1,
    )
    return sched


def build_scheduler(scheduler_cls=BackgroundScheduler):
    """构造带时区与任务默认值的调度器并注册任务（不启动、不加锁）。"""
    sched = scheduler_cls(timezone=SCHEDULER_TZ, job_defaults=dict(JOB_DEFAULTS))
    return register_jobs(sched)


_STARTED_MSG = (
    "APScheduler 已启动 "
    "(08:50 增量日线 / 09-14 */5 分钟 / 15:10 收盘汇总 / "
    "15:30-21:00 */30 晚到行情预热 / 17:15 探测器扫描 / 02:30 分钟线清理)"
)


def _start_web_scheduler_locked():
    """已持有调度锁：构造并启动 Web 内调度器。调用方需持有 _start_mutex。"""
    global scheduler
    scheduler = attach_heartbeat(build_scheduler(), mode='web')
    scheduler.start()
    write_heartbeat(scheduler)
    logger.info(_STARTED_MSG + "，运行于 Web 进程（兼容模式）")


def _retry_lock_loop(interval):
    """后台线程：每 interval 秒重试一次调度锁，拿到即启动调度器；stop() 置位后退出。"""
    while not _retry_stop.wait(interval):
        if not _acquire_lock():
            logger.warning(f"调度锁仍被其他进程持有，{interval}s 后重试")
            continue
        with _start_mutex:
            if _retry_stop.is_set() or scheduler is not None:
                _release_lock()
                return
            _start_web_scheduler_locked()
        return


def start(retry_seconds=None):
    """Web 进程内启动 APScheduler（兼容模式；白名单启动条件 + 单实例文件锁）。

    锁被占用（如独立调度进程或另一个 worker 持有）时不再只试一次就放弃，
    而是起后台线程每 LOCK_RETRY_SECONDS 秒重试并记 WARNING（REL-03）。
    """
    global _retry_thread, _atexit_registered
    if not _should_start_scheduler():
        logger.debug("跳过 APScheduler 启动（非 Web 服务进程或环境禁用）")
        return

    interval = LOCK_RETRY_SECONDS if retry_seconds is None else retry_seconds
    with _start_mutex:
        if scheduler is not None or (_retry_thread is not None and _retry_thread.is_alive()):
            return
        if not _atexit_registered:
            atexit.register(stop)
            _atexit_registered = True
        _retry_stop.clear()
        if _acquire_lock():
            _start_web_scheduler_locked()
            return
        logger.warning(f"APScheduler 暂未启动：其他进程已持有调度锁，后台每 {interval}s 重试")
        _retry_thread = threading.Thread(
            target=_retry_lock_loop, args=(interval,), daemon=True, name='scheduler-lock-retry',
        )
        _retry_thread.start()


def stop():
    global scheduler
    _retry_stop.set()
    with _start_mutex:
        if scheduler is not None:
            try:
                scheduler.shutdown(wait=False)
            except Exception:
                pass
            write_heartbeat(scheduler, running=False)
            scheduler = None
        _release_lock()


def run_forever(stop_event, lock_retry_seconds=60):
    """独立调度进程主循环（manage.py run_scheduler 调用）：阻塞直到 stop_event 被置位。

    - 拿不到调度锁（例如兼容模式下 Web 进程已持有）时每 lock_retry_seconds 秒重试并记 WARNING，
      而不是直接退出——持锁方退出后本进程自动接管。
    - 退出时 shutdown(wait=True)：等正在跑的任务结束（systemd TimeoutStopSec 兜底），
      不再像 Web worker 回收那样把长任务拦腰截断。
    返回 True 表示正常启动过调度器；False 表示在拿到锁之前就被要求退出。
    """
    global scheduler
    while not _acquire_lock():
        logger.warning(f"调度锁被其他进程持有，{lock_retry_seconds}s 后重试")
        if stop_event.wait(lock_retry_seconds):
            return False

    sched = attach_heartbeat(build_scheduler(), mode='process')
    scheduler = sched
    try:
        sched.start()
        write_heartbeat(sched)
        logger.info(_STARTED_MSG + f"，独立进程 pid={os.getpid()}")
        stop_event.wait()
        logger.info("收到退出信号，等待正在执行的任务结束后关闭调度器")
    finally:
        try:
            if sched.running:
                sched.shutdown(wait=True)
        except Exception:
            logger.exception("调度器关闭异常")
        write_heartbeat(sched, running=False)
        scheduler = None
        _release_lock()
    logger.info("APScheduler 已停止")
    return True
