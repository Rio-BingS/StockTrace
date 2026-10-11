"""同步拉上游的总时限与熔断（REL-05），从 stocks/views.py 迁入（ARC-01）。

请求线程把 loader 交给有界线程池，最多等 STOCKTRACE_REQUEST_DEADLINE 秒（默认 100，
小于 nginx proxy_read_timeout 120s），超时抛 UpstreamTimeout 而不是让 nginx 先断开、
worker 继续空等。同一 label 连续 3 次超时即熔断 60s，期间直接抛 UpstreamCircuitOpen，
避免挂死的上游把线程池占满。HTTP 映射（504/503）由视图层负责。
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from django.conf import settings
from django.db import close_old_connections

from ..errors import UpstreamCircuitOpen, UpstreamTimeout  # 定义已迁至 stocks.errors（ARC-05）

logger = logging.getLogger(__name__)

_UPSTREAM_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix='upstream')
_BREAKER_THRESHOLD = 3
_BREAKER_OPEN_SECONDS = 60
_breaker_lock = threading.Lock()
_breaker = {}  # label -> {'timeouts': 连续超时次数, 'open_until': monotonic 秒}


def _request_deadline():
    return float(getattr(settings, 'STOCKTRACE_REQUEST_DEADLINE', 100))


def _run_loader(loader):
    try:
        return loader()
    finally:
        close_old_connections()  # 池线程长驻：用完归还 DB 连接


def _call_with_deadline(loader, label):
    """在线程池里执行 loader，带总时限与按 label 熔断。超时抛 UpstreamTimeout，熔断中抛 UpstreamCircuitOpen。"""
    now = time.monotonic()
    with _breaker_lock:
        st = _breaker.get(label)
        if st and st['open_until'] > now:
            raise UpstreamCircuitOpen(label)
    future = _UPSTREAM_POOL.submit(_run_loader, loader)
    try:
        result = future.result(timeout=_request_deadline())
    except FutureTimeout:
        future.cancel()  # 还在排队则不再执行；已在跑的线程无法强停，由上游 socket 超时兜底
        with _breaker_lock:
            st = _breaker.setdefault(label, {'timeouts': 0, 'open_until': 0.0})
            st['timeouts'] += 1
            if st['timeouts'] >= _BREAKER_THRESHOLD:
                st['open_until'] = time.monotonic() + _BREAKER_OPEN_SECONDS
                st['timeouts'] = 0
                logger.warning(f'{label} 连续 {_BREAKER_THRESHOLD} 次超时，熔断 {_BREAKER_OPEN_SECONDS}s')
        raise UpstreamTimeout(label)
    with _breaker_lock:
        _breaker.pop(label, None)
    return result


# 公开名（新代码用这些）；下划线旧名保留一版
call_with_deadline = _call_with_deadline
request_deadline = _request_deadline
BREAKER_THRESHOLD = _BREAKER_THRESHOLD
BREAKER_OPEN_SECONDS = _BREAKER_OPEN_SECONDS
