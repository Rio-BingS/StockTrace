"""测试隔离 runner（OPS-03）：断网闸 + 每个用例前清空进程内缓存。

- 测试期间 patch socket.connect/connect_ex/sendto 与 getaddrinfo/gethostbyname，
  只放行回环地址与 AF_UNIX；其余一律抛 NetworkBlockedError 并记录调用点。
- 项目代码普遍 ``except Exception`` 吞掉上游异常，单靠抛错用例未必会红，
  因此用 result mixin 在 stopTest 时把发生过网络尝试的用例判失败；
  用例之外（setUpClass、模块导入等）的尝试在结尾列出，并使整次运行失败。
- 每个用例开始前清空 market/_sources/search 等模块级缓存，避免用例间串扰。
- STOCKTRACE_TEST_ALLOW_NETWORK=1 关闭断网闸（仅本地临时排查用）。
- 测试期间 STOCKTRACE_MARKET_CALENDAR_ENABLED=False：市场缓存不查交易日历（ARC-02）；
  STOCKTRACE_FLOAT_SHARES_ENABLED=False：探测器不拉流通股本（ALG-13）。

注意：本模块名不能以 test 开头，否则会被测试发现当成测试模块导入。
"""

from __future__ import annotations

import importlib
import ipaddress
import os
import socket
import sys
import threading
import traceback
from collections import Counter
from pathlib import Path

from django.test.runner import DiscoverRunner
from django.test.utils import override_settings

_BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
_THIS_FILE = str(Path(__file__).resolve())


class NetworkBlockedError(OSError):
    """测试中禁止访问外网。"""


def _network_allowed_by_env():
    return os.environ.get('STOCKTRACE_TEST_ALLOW_NETWORK', '').lower() in ('1', 'true', 'yes')


def _is_loopback_host(host):
    if host is None:
        return True  # getaddrinfo(None, port) 只解析本地通配地址
    if isinstance(host, bytes):
        host = host.decode('ascii', 'ignore')
    host = str(host).strip('[]')
    if host.lower() in ('localhost', 'localhost.localdomain', ''):
        return True
    try:
        return ipaddress.ip_address(host.split('%')[0]).is_loopback
    except ValueError:
        return False


def _call_site():
    """取最内层的项目代码帧（backend/ 下、本文件以外）作为调用点。"""
    frames = [
        f for f in traceback.extract_stack()
        if f.filename.startswith(_BACKEND_DIR) and f.filename != _THIS_FILE
    ]
    if not frames:
        return '<非项目代码>'
    inner = frames[-1]
    return f'{os.path.relpath(inner.filename, _BACKEND_DIR)}:{inner.lineno} ({inner.name})'


def _format_hits(hits):
    """同一 (目标, 调用点) 合并计数，避免重试循环刷屏。"""
    return '\n'.join(
        f'  - {what}  @ {site}' + (f'  ×{n}' if n > 1 else '')
        for (what, site), n in Counter(hits).items()
    )


# ============================================================
# 断网闸
# ============================================================

class _NetworkGuard:
    def __init__(self):
        self._lock = threading.Lock()
        self._orig = {}
        self.current_test = None
        self.test_hits = []      # 当前用例内的尝试
        self.outside_hits = []   # 用例之外的尝试：(描述, 调用点)

    def _record(self, what):
        site = _call_site()
        with self._lock:
            if self.current_test is not None:
                self.test_hits.append((what, site))
            else:
                self.outside_hits.append((what, site))
        raise NetworkBlockedError(f'测试中禁止访问网络：{what}（调用点 {site}）')

    def install(self):
        if self._orig:
            return
        guard = self
        orig = self._orig
        orig['connect'] = socket.socket.connect
        orig['connect_ex'] = socket.socket.connect_ex
        orig['sendto'] = socket.socket.sendto
        orig['getaddrinfo'] = socket.getaddrinfo
        orig['gethostbyname'] = socket.gethostbyname

        def _addr_allowed(sock, address):
            if sock.family == getattr(socket, 'AF_UNIX', None):
                return True
            host = address[0] if isinstance(address, (tuple, list)) and address else address
            return _is_loopback_host(host)

        def connect(self, address):
            if not _addr_allowed(self, address):
                guard._record(f'connect {address!r}')
            return orig['connect'](self, address)

        def connect_ex(self, address):
            if not _addr_allowed(self, address):
                guard._record(f'connect_ex {address!r}')
            return orig['connect_ex'](self, address)

        def sendto(self, data, *args):
            address = args[-1] if args else None
            if not _addr_allowed(self, address):
                guard._record(f'sendto {address!r}')
            return orig['sendto'](self, data, *args)

        def getaddrinfo(host, *args, **kwargs):
            if not _is_loopback_host(host):
                guard._record(f'getaddrinfo {host!r}')
            return orig['getaddrinfo'](host, *args, **kwargs)

        def gethostbyname(host):
            if not _is_loopback_host(host):
                guard._record(f'gethostbyname {host!r}')
            return orig['gethostbyname'](host)

        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex
        socket.socket.sendto = sendto
        socket.getaddrinfo = getaddrinfo
        socket.gethostbyname = gethostbyname

    def uninstall(self):
        if not self._orig:
            return
        socket.socket.connect = self._orig['connect']
        socket.socket.connect_ex = self._orig['connect_ex']
        socket.socket.sendto = self._orig['sendto']
        socket.getaddrinfo = self._orig['getaddrinfo']
        socket.gethostbyname = self._orig['gethostbyname']
        self._orig = {}

    def begin_test(self, test):
        with self._lock:
            self.current_test = test
            self.test_hits = []

    def end_test(self):
        with self._lock:
            hits, self.test_hits = self.test_hits, []
            self.current_test = None
        return hits


_guard = _NetworkGuard()


# ============================================================
# 进程内缓存清理
# ============================================================

# (模块, 属性, 复位值)：复位值为 None 表示 dict/OrderedDict 原地 clear；
# 其他值表示原地 update（dict）或直接重新赋值（标量）。按名字存在与否容错。
_CACHE_RESETS = [
    ('stocks.market._cache', '_cache', None),
    ('stocks.market._sources', '_source_state', None),
    ('stocks.search', '_stock_list_cache', {'df': None, 'expires': 0}),
    ('stocks.trading_calendar', '_trade_day_cache', None),
    ('stocks.trading_calendar', '_trade_calendar_set', ('set', None)),
    ('stocks.trading_calendar', '_trade_calendar_range', ('set', None)),
    ('stocks.trading_calendar', '_trade_calendar_expires', ('set', 0)),
    ('stocks.trading_calendar', '_calendar_fail_until', {'sina': 0, 'baostock': 0}),
    ('stocks.trading_calendar', '_session_close_memo', {'close': None, 'at': 0.0}),
    ('stocks.ai.valuation', '_last_fail_at', ('set', 0.0)),
    ('stocks.market.sectors', '_ths_token', {'v': None, 'at': 0.0}),
    ('stocks.market.snapshots', '_etf_share_memo', {'version': None, 'values': {}}),
    ('stocks.datasources.float_shares', '_cache', None),
    ('stocks.datasources.float_shares', '_fail_state', {'count': 0, 'until': 0.0}),
]


def reset_process_caches():
    for mod_name, attr, value in _CACHE_RESETS:
        mod = sys.modules.get(mod_name)
        if mod is None:
            try:
                mod = importlib.import_module(mod_name)
            except ImportError:
                continue
        if not hasattr(mod, attr):
            continue
        if isinstance(value, tuple) and value and value[0] == 'set':
            setattr(mod, attr, value[1])
            continue
        obj = getattr(mod, attr)
        obj.clear()
        if value:
            obj.update(value)


# ============================================================
# result mixin + runner
# ============================================================

class NetworkGuardResultMixin:
    def startTest(self, test):
        reset_process_caches()
        _guard.begin_test(test)
        super().startTest(test)

    def stopTest(self, test):
        hits = _guard.end_test()
        if hits:
            detail = _format_hits(hits)
            try:
                raise AssertionError(f'用例尝试访问网络 {len(hits)} 次（须补 mock）：\n{detail}')
            except AssertionError:
                self.addFailure(test, sys.exc_info())
        super().stopTest(test)


class IsolatedTestRunner(DiscoverRunner):
    """断网 + 缓存隔离的 DiscoverRunner（settings.TEST_RUNNER）。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.network_guard_enabled = not _network_allowed_by_env()
        if self.network_guard_enabled and self.parallel > 1:
            # 并行 worker 不走本进程的 result 类，无法按用例判失败：强制串行
            print('IsolatedTestRunner：断网闸只支持串行运行，忽略 --parallel', file=sys.stderr)
            self.parallel = 1

    def get_resultclass(self):
        base = super().get_resultclass()
        if not self.network_guard_enabled:
            return base
        if base is None:
            from unittest import TextTestResult as base
        return type(f'NetworkGuarded{base.__name__}', (NetworkGuardResultMixin, base), {})

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        # 市场缓存新鲜度不查交易日历（避免触网、保持确定性）；需要的用例自行 patch
        # 探测器流通股本同理（ALG-13）：默认不取，需要的用例自行打开并 mock
        self._calendar_override = override_settings(
            STOCKTRACE_MARKET_CALENDAR_ENABLED=False, STOCKTRACE_FLOAT_SHARES_ENABLED=False,
        )
        self._calendar_override.enable()
        if self.network_guard_enabled:
            _guard.outside_hits = []
            _guard.install()
        else:
            print('IsolatedTestRunner：STOCKTRACE_TEST_ALLOW_NETWORK=1，断网闸已关闭', file=sys.stderr)

    def teardown_test_environment(self, **kwargs):
        _guard.uninstall()
        override = getattr(self, '_calendar_override', None)
        if override is not None:
            override.disable()
        super().teardown_test_environment(**kwargs)

    def suite_result(self, suite, result, **kwargs):
        failures = super().suite_result(suite, result, **kwargs)
        if self.network_guard_enabled and _guard.outside_hits:
            print(f'\n==== 用例之外的网络尝试 {len(_guard.outside_hits)} 次（运行判失败）====',
                  file=sys.stderr)
            print(_format_hits(_guard.outside_hits), file=sys.stderr)
            failures += 1
        return failures
