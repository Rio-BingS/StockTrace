"""
任务互斥锁（REL-04）：同名任务在本机只允许一个执行者。

两层锁：
1. 进程内 threading.Lock（按名单例）——同进程的请求线程、后台线程、进程内调度互斥；
2. fcntl.flock 文件锁 <STOCKTRACE_RUN_DIR>/task-<name>.lock——Web 进程、独立调度进程、
   管理命令（fetch_stock_data / run_detector_scan）之间互斥。进程退出（含被强杀）时内核自动释放。

acquire(timeout)/release 可以在不同线程调用：请求线程非阻塞取锁后把锁交给后台线程，
由后台线程在任务结束时释放（threading.Lock 允许跨线程 release）。
锁目录不可用（权限/不存在且无法创建）或平台无 fcntl 时退化为仅进程内锁，并记 WARNING。

已用锁名：stock_quotes（自选行情写入）、detector_scan、market_snapshots、minute_bar_cleanup、
index_constituents、pool_quotes（成分池限速采集）。
"""

import logging
import os
import threading
import time
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # Windows 本地开发：只有进程内锁
    fcntl = None

logger = logging.getLogger(__name__)

# 文件锁轮询间隔（秒）；flock 本身没有超时参数，限时等待靠非阻塞重试
_POLL_SECONDS = 0.2

_registry_lock = threading.Lock()
_states = {}


class _State:
    __slots__ = ('name', 'thread_lock', 'fd')

    def __init__(self, name):
        self.name = name
        self.thread_lock = threading.Lock()
        self.fd = None  # 持锁期间的文件描述符；None 表示未持有文件锁（或已退化）


def _state_for(name):
    with _registry_lock:
        st = _states.get(name)
        if st is None:
            st = _states[name] = _State(name)
        return st


def lock_path(name):
    from django.conf import settings

    run_dir = getattr(settings, 'STOCKTRACE_RUN_DIR', None) or settings.BASE_DIR
    return os.path.join(str(run_dir), f'task-{name}.lock')


def _open_lock_file(path):
    """打开（必要时创建）锁文件。只读打开即可 flock：root 跑命令建出的 0644 文件，
    www 进程仍能打开加锁，不会出现"root 属主锁文件把服务锁死"。"""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    return os.open(path, os.O_RDONLY | os.O_CREAT, 0o664)


class TaskLock:
    """按名任务锁。timeout=None 无限等待；0 非阻塞；>0 最多等待该秒数。"""

    def __init__(self, name):
        self.name = name
        self._st = _state_for(name)

    def acquire(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        if timeout is None:
            got = self._st.thread_lock.acquire()
        elif timeout <= 0:
            got = self._st.thread_lock.acquire(blocking=False)
        else:
            got = self._st.thread_lock.acquire(timeout=timeout)
        if not got:
            return False

        if fcntl is None:
            return True
        path = lock_path(self.name)
        try:
            fd = _open_lock_file(path)
        except OSError as e:
            logger.warning('任务锁目录不可用（%s: %s），%s 退化为进程内锁', path, e, self.name)
            return True

        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._st.fd = fd
                return True
            except BlockingIOError:
                pass
            except OSError as e:
                os.close(fd)
                logger.warning('任务文件锁不可用（%s: %s），%s 退化为进程内锁', path, e, self.name)
                return True
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                os.close(fd)
                self._st.thread_lock.release()
                return False
            time.sleep(_POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining))

    def release(self):
        fd, self._st.fd = self._st.fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                os.close(fd)
        self._st.thread_lock.release()

    def locked(self):
        """本进程内是否有人持有该锁（不探测其他进程）。"""
        return self._st.thread_lock.locked()


@contextmanager
def task_lock(name, timeout=None):
    """上下文管理器：yield 是否拿到锁；拿到时退出自动释放。

        with task_lock('stock_quotes', timeout=0) as ok:
            if not ok:
                return  # 忙，跳过
            ...
    """
    lock = TaskLock(name)
    ok = lock.acquire(timeout)
    try:
        yield ok
    finally:
        if ok:
            lock.release()
