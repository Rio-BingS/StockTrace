"""
进程内后台拉取任务（daemon thread）。

适合单机 1H2G、不引入 Celery。Gunicorn 多 worker 时状态不共享——
前端轮询应打到同一 worker 或接受「某次请求看不到 running」后重试；
本实现以「触发后的短时轮询 + 看板刷新」为主，足够个人自选规模。

互斥（REL-04）：所有写自选行情的入口（这里的手动拉取、定时任务、fetch_stock_data 命令）
共用任务锁 stock_quotes（进程内锁 + 文件锁，见 task_lock.py）。请求线程只做非阻塞取锁，
拿到后交给后台线程，由后台线程在结束时释放；拿不到即返回忙，不在请求线程里等待。
"""

import itertools
import logging
import threading
import time

from django.utils import timezone

from .task_lock import TaskLock

logger = logging.getLogger(__name__)

FETCH_LOCK_NAME = 'stock_quotes'

# 看门狗阈值（秒）：running 超过该时长视为线程疑似挂死（上游无响应等）。
# 只标记 stale 并告警，不复位、不并发启动新线程——旧线程仍持有任务锁，
# 另起一个只会和它同时写库（REL-04）；旧线程真正结束时才释放锁并复位状态。
_STALE_AFTER_SECONDS = {'one': 10 * 60, 'all': 30 * 60}

# 新增关注时拉取锁被其他进程（调度/命令）占用：后台线程最多等这么久再放弃
_PENDING_LOCK_WAIT_SECONDS = 15 * 60

_lock = threading.Lock()
_state = {
    'running': False,
    'task': None,  # 'one' | 'all' | None
    'code': None,
    'run_id': None,
    'stale': False,
    'last_started': None,
    'last_finished': None,
    'last_error': None,
    'last_results': None,
    'last_status': 'idle',  # idle | running | success | error
}
_started_at = None  # time.monotonic() 秒，配合 running 判断是否挂死
_pending = set()  # 忙时新增关注的 stock_id，当前任务结束后由同一后台线程补拉
_run_ids = itertools.count(1)


def get_fetch_status():
    with _lock:
        status = dict(_state)
        status['pending'] = sorted(_pending)
        if status['running'] and _started_at is not None:
            status['running_seconds'] = int(time.monotonic() - _started_at)
        return status


def _set(run_id, **kwargs):
    """只有 run_id 与当前运行一致时才写回状态，防止过期线程覆盖新状态。"""
    with _lock:
        if _state['run_id'] != run_id:
            return
        _state.update(kwargs)


def _snapshot_locked():
    snap = dict(_state)
    snap['pending'] = sorted(_pending)
    return snap


def _recover_stale_locked():
    """running 超过阈值视为疑似挂死：只标记 stale 并告警。调用方需已持有 _lock。"""
    if not _state['running'] or _started_at is None or _state['stale']:
        return
    limit = _STALE_AFTER_SECONDS.get(_state['task'], _STALE_AFTER_SECONDS['all'])
    elapsed = time.monotonic() - _started_at
    if elapsed <= limit:
        return
    logger.warning(
        "拉取任务 %s 已运行 %d 秒（阈值 %d 秒），疑似挂死；仅标记，不并发启动新任务",
        _state['task'], int(elapsed), limit,
    )
    _state.update(
        stale=True,
        last_error=f'任务运行 {int(elapsed)} 秒未结束，疑似挂死（等待其结束或重启服务）',
    )


def _claim_locked(task, code=None):
    """标记新一轮运行开始并返回 run_id。调用方需已持有 _lock。"""
    global _started_at
    run_id = next(_run_ids)
    _state.update(
        running=True,
        task=task,
        code=code,
        run_id=run_id,
        stale=False,
        last_started=timezone.now().isoformat(),
        last_finished=None,
        last_error=None,
        last_status='running',
    )
    _started_at = time.monotonic()
    return run_id


def _finish(run_id, **kwargs):
    global _started_at
    with _lock:
        if _state['run_id'] != run_id:
            return
        _state.update(running=False, task=None, stale=False, last_finished=timezone.now().isoformat(), **kwargs)
        _started_at = None


def _do_one(run_id, stock_id, light):
    from .models import Stock
    from .jobs import fetch_stock_all_data

    stock = Stock.objects.get(pk=stock_id)
    _set(run_id, code=stock.code)
    count = fetch_stock_all_data(stock, light=light)
    return {'code': stock.code, 'count': count}


def _do_all(run_id, light):
    from .jobs import fetch_all_active_stocks

    return fetch_all_active_stocks(light=light)


def _drain_pending(run_id):
    """补拉排队中的新增关注（light）；单只失败只记日志。"""
    done = []
    while True:
        with _lock:
            if not _pending:
                return done
            stock_id = _pending.pop()
            _state['task'] = 'one'
        try:
            done.append(_do_one(run_id, stock_id, True))
        except Exception:  # 边界：单只补拉失败不影响其余排队股票
            logger.exception("补拉排队股票 %s 失败", stock_id)


def _worker(run_id, job, args, lock, lock_held):
    """后台线程：（必要时等锁）→ 执行任务 → 补拉排队 → 释放锁。"""
    try:
        if not lock_held:
            if not lock.acquire(timeout=_PENDING_LOCK_WAIT_SECONDS):
                with _lock:
                    _pending.clear()
                _finish(run_id, last_status='error', last_error='拉取锁长时间被其他任务占用，已放弃')
                return
            lock_held = True
        result = job(run_id, *args)
        extra = _drain_pending(run_id)
        if extra:
            result = {'main': result, 'pending': extra}
        _finish(run_id, last_results=result, last_status='success', last_error=None)
    except Exception as e:  # 后台线程边界：任何异常都要落到任务状态里，否则前端一直显示运行中
        logger.exception("后台拉取任务 %s 失败", job.__name__)
        try:
            _drain_pending(run_id)
        except Exception:
            logger.exception("补拉排队股票失败")
        _finish(run_id, last_status='error', last_error=str(e))
    finally:
        if lock_held:
            lock.release()


def _start(task, job, args, queue_stock_id=None):
    """公共启动逻辑。返回 (started, snapshot)；snapshot['queued'] 表示已排队。"""
    with _lock:
        _recover_stale_locked()
        if _state['running']:
            if queue_stock_id is not None:
                _pending.add(queue_stock_id)
            snap = _snapshot_locked()
            snap['queued'] = queue_stock_id is not None
            return False, snap

        lock = TaskLock(FETCH_LOCK_NAME)
        lock_held = lock.acquire(timeout=0)  # 请求线程绝不阻塞等锁
        if not lock_held and queue_stock_id is None:
            snap = _snapshot_locked()
            snap['locked_by_other'] = True
            return False, snap
        run_id = _claim_locked(task)
        snap = _snapshot_locked()
        if not lock_held:
            snap['queued'] = True  # 其他进程在写行情：后台线程等锁后再拉

    t = threading.Thread(target=_worker, args=(run_id, job, args, lock, lock_held), daemon=True)
    try:
        t.start()
    except Exception:  # 只做清理后原样抛出
        if lock_held:
            lock.release()
        _finish(run_id, last_status='error', last_error='后台线程启动失败')
        raise
    return True, snap


def start_fetch_one(stock_id, light=True, queue_if_busy=False):
    """启动单只后台拉取。默认 light。

    已有任务在跑（或拉取锁被调度/命令占用）时返回 (False, 状态)；
    queue_if_busy=True（新增关注）时改为把 stock_id 放入待办，当前任务结束后补拉，
    返回的状态里 queued=True，前端据此提示"已排队"。
    """
    return _start('one', _do_one, (stock_id, light), queue_stock_id=stock_id if queue_if_busy else None)


def start_fetch_all(light=True):
    """启动全部后台拉取。默认 light，减少突发大量请求。忙则返回 (False, 状态)。"""
    return _start('all', _do_all, (light,))
