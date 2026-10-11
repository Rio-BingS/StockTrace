"""P1 测试隔离回归（OPS-03）：断网闸拦截外网、放行回环，进程内缓存逐用例清空。

断网闸只在 IsolatedTestRunner 下生效；STOCKTRACE_TEST_ALLOW_NETWORK=1 或换用
其他 runner 时整类跳过。触发拦截后须清掉本用例的记录，否则会被 runner 判失败。
"""

import io
import socket
import unittest

from django.test import SimpleTestCase

from StockTrace import isolated_runner
from StockTrace.isolated_runner import (
    NetworkBlockedError,
    NetworkGuardResultMixin,
    _guard,
    reset_process_caches,
)


def _guard_active():
    return bool(_guard._orig)


@unittest.skipUnless(_guard_active(), '断网闸未启用（非 IsolatedTestRunner 或已用环境变量关闭）')
class NetworkGuardTests(SimpleTestCase):
    def _discard_hits(self):
        # 预期内的拦截：清掉记录，免得 stopTest 判本用例失败
        _guard.test_hits.clear()

    def test_external_resolution_blocked_and_recorded(self):
        with self.assertRaises(NetworkBlockedError):
            socket.getaddrinfo('push2.eastmoney.com', 443)
        with self.assertRaises(NetworkBlockedError):
            socket.gethostbyname('qt.gtimg.cn')
        self.assertEqual(len(_guard.test_hits), 2)
        what, site = _guard.test_hits[0]
        self.assertIn('push2.eastmoney.com', what)
        self.assertIn('tests_p1_tests_ci.py', site)  # 调用点指向项目代码
        self._discard_hits()

    def test_external_connect_blocked_and_is_oserror(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(OSError):  # 被项目里的 except OSError/Exception 正常兜住
                s.connect(('203.0.113.10', 80))
            with self.assertRaises(NetworkBlockedError):
                s.connect_ex(('203.0.113.10', 80))
        finally:
            s.close()
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with self.assertRaises(NetworkBlockedError):
                u.sendto(b'x', ('203.0.113.10', 53))
        finally:
            u.close()
        self.assertEqual(len(_guard.test_hits), 3)
        self._discard_hits()

    def test_loopback_and_unix_allowed(self):
        socket.getaddrinfo('localhost', 80)
        socket.getaddrinfo('127.0.0.1', 80)
        socket.gethostbyname('127.0.0.1')
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(('127.0.0.1', 0))
        srv.listen(1)
        cli = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            cli.connect(srv.getsockname())
        finally:
            cli.close()
            srv.close()
        a, b = socket.socketpair(socket.AF_UNIX)
        a.close()
        b.close()
        self.assertEqual(_guard.test_hits, [])

    def test_swallowed_attempt_still_fails_the_test(self):
        # 项目代码普遍 except Exception 吞掉上游异常：用例本身「通过」也要判失败
        class _Inner(unittest.TestCase):
            def runTest(self):
                try:
                    socket.getaddrinfo('push2.eastmoney.com', 443)
                except Exception:
                    pass

        result_cls = type('R', (NetworkGuardResultMixin, unittest.TextTestResult), {})
        result = result_cls(io.StringIO(), descriptions=False, verbosity=0)
        try:
            _Inner().run(result)
        finally:
            _guard.begin_test(self)  # 嵌套运行后恢复外层用例的记录上下文
        self.assertEqual(len(result.failures), 1)
        self.assertIn('push2.eastmoney.com', result.failures[0][1])


class ProcessCacheResetTests(SimpleTestCase):
    def test_reset_clears_module_level_caches(self):
        from stocks import trading_calendar
        from stocks.ai import valuation
        from stocks.market import _sources
        from stocks.market._cache import _cache, _cache_set

        _cache_set('k', 1)
        _sources._source_state['src'] = {'fails': 9, 'cool_until': 9e18}
        trading_calendar._trade_day_cache['d'] = (True, 9e18)
        trading_calendar._trade_calendar_set = {1}
        trading_calendar._calendar_fail_until['sina'] = 9e18
        valuation._last_fail_at = 9e18

        reset_process_caches()

        self.assertEqual(len(_cache), 0)
        self.assertEqual(_sources._source_state, {})
        self.assertEqual(trading_calendar._trade_day_cache, {})
        self.assertIsNone(trading_calendar._trade_calendar_set)
        self.assertEqual(trading_calendar._calendar_fail_until['sina'], 0)
        self.assertEqual(valuation._last_fail_at, 0.0)

    def test_reset_targets_all_exist(self):
        # 缓存改名后 reset 会静默跳过：这里锁住清单与代码一致
        import importlib

        for mod_name, attr, _value in isolated_runner._CACHE_RESETS:
            self.assertTrue(hasattr(importlib.import_module(mod_name), attr), f'{mod_name}.{attr}')
