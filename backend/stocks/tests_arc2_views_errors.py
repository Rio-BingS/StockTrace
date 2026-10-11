"""ARC2 views-errors 方向的新增测试（ARC-01 / ARC-04 / ARC-05）。"""

from datetime import date

from django.test import SimpleTestCase

from .common.params import parse_bool, parse_date, positive_int


class PositiveIntTests(SimpleTestCase):
    def test_lenient_default_falls_back(self):
        # detector/views 旧口径：非法值静默回退默认值
        for raw in ('abc', '0', '-3', '1.5'):
            with self.subTest(raw=raw):
                self.assertEqual(positive_int({'page': raw}, 'page', 7), 7)
        self.assertEqual(positive_int({}, 'page', 7), 7)
        self.assertEqual(positive_int({'page': ''}, 'page', 7), 7)
        self.assertEqual(positive_int({'page': '3'}, 'page', 7), 3)
        self.assertEqual(positive_int({'page': '300'}, 'page', 7, maximum=100), 100)

    def test_strict_raises_with_legacy_message(self):
        # stocks/views 旧口径：非法值抛 ValueError（视图转 400），文案不变
        for raw in ('abc', '0', '-3'):
            with self.subTest(raw=raw), self.assertRaisesMessage(ValueError, 'page_size 必须是正整数'):
                positive_int({'page_size': raw}, 'page_size', 50, strict=True)
        self.assertEqual(positive_int({'page_size': ''}, 'page_size', 50, strict=True), 50)
        self.assertEqual(positive_int({'page_size': '999'}, 'page_size', 50, 100, strict=True), 100)


class ParseBoolTests(SimpleTestCase):
    def test_default_truthy_set_ignores_case(self):
        for raw in ('1', 'true', 'TRUE', 'Yes', True, 1):
            with self.subTest(raw=raw):
                self.assertTrue(parse_bool(raw))
        for raw in (None, '', '0', 'false', 'on', 'nope', False):
            with self.subTest(raw=raw):
                self.assertFalse(parse_bool(raw))

    def test_custom_truthy_case_sensitive(self):
        kw = {'truthy': ('1', 'true'), 'ignore_case': False}
        self.assertTrue(parse_bool('true', **kw))
        self.assertFalse(parse_bool('TRUE', **kw))
        self.assertFalse(parse_bool('yes', **kw))

    def test_strict(self):
        self.assertFalse(parse_bool('no', strict=True))
        with self.assertRaisesMessage(ValueError, 'full 必须是布尔值'):
            parse_bool('maybe', strict=True, key='full')
        self.assertTrue(parse_bool(None, default=True, strict=True))


class ParseDateTests(SimpleTestCase):
    def test_lenient_and_strict(self):
        self.assertEqual(parse_date('2026-09-04'), date(2026, 9, 4))
        self.assertIsNone(parse_date('2026/09/04'))
        self.assertIsNone(parse_date(''))
        self.assertEqual(parse_date('bad', default=date(2026, 1, 1)), date(2026, 1, 1))
        with self.assertRaisesMessage(ValueError, 'end 格式应为 YYYY-MM-DD'):
            parse_date('2026/09/04', 'end', strict=True)
        self.assertIsNone(parse_date(None, strict=True))

    def test_detector_legacy_helpers_match_lenient_mode(self):
        """detector/views 的旧实现与 strict=False 结果一致：集成时可直接替换。"""
        from types import SimpleNamespace

        from .detector import views as dv

        if not (hasattr(dv, '_parse_date') and hasattr(dv, '_positive_int')):
            self.skipTest('detector/views 已改用 common.params，旧实现已删除')
        for raw in ('2026-09-04', '2026/09/04', 'x', None):
            with self.subTest(raw=raw):
                self.assertEqual(dv._parse_date(raw), parse_date(raw))
        for raw in (None, '', 'abc', '0', '-1', '5', '500'):
            with self.subTest(raw=raw):
                params = {} if raw is None else {'page': raw}
                req = SimpleNamespace(query_params=params)
                self.assertEqual(dv._positive_int(req, 'page', 1), positive_int(params, 'page', 1))
                self.assertEqual(dv._positive_int(req, 'page', 1, 100), positive_int(params, 'page', 1, 100))


class EnvelopeTests(SimpleTestCase):
    def test_default_order(self):
        from .common.envelope import envelope

        body = envelope([1], {'page': 1}, {'m': 1}, extra=2)
        self.assertEqual(list(body), ['items', 'pagination', 'meta', 'extra'])

    def test_key_order_keeps_legacy_layout_and_rejects_mismatch(self):
        from .common.envelope import envelope

        body = envelope([], None, {}, key_order=('a', 'meta', 'items', 'pagination'), a=1)
        self.assertEqual(list(body), ['a', 'meta', 'items', 'pagination'])
        with self.assertRaises(ValueError):
            envelope([], key_order=('items', 'meta'))
        with self.assertRaises(ValueError):
            envelope([], key_order=('items', 'pagination', 'meta', 'ghost'))


class ResolvePeriodYtdTests(SimpleTestCase):
    """FULL_PRESETS 含 ytd，但 _PRESET_DAYS 缺项导致 KeyError（大盘资金流/北向 ?period=ytd 502）。"""

    def test_ytd_preset_no_longer_keyerror(self):
        from .market.periods import FULL_PRESETS, resolve_period

        today = date(2026, 9, 4)
        w = resolve_period(lambda k: {'period': 'ytd'}.get(k), FULL_PRESETS, today=today)
        self.assertEqual((w.preset, w.start, w.end), ('ytd', date(2026, 1, 1), today))

    def test_market_flow_ytd_endpoint_resolves(self):
        from unittest import mock

        with mock.patch('stocks.views.market.get_market_fund_flow_window', return_value={'ok': 1}) as m:
            r = self.client.get('/api/market/market-flow/?period=ytd')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(m.call_args.args[0].preset, 'ytd')


class ResolvePeriodExtensionTests(SimpleTestCase):
    """步骤 4b：resolve_period 新增 max_days / allow_future_end / ytd / 文案覆盖。"""

    TODAY = date(2026, 9, 4)

    def _resolve(self, params, allowed=('1w', '1m', 'ytd'), **kw):
        from .market.periods import resolve_period

        return resolve_period(lambda k: params.get(k), list(allowed), today=self.TODAY, **kw)

    def test_max_days_overrides_legacy_span(self):
        params = {'start': '2026-01-01', 'end': '2026-09-01'}
        with self.assertRaisesMessage(ValueError, '自定义区间最长 100 天'):
            self._resolve(params, max_days=100)
        self.assertEqual(self._resolve(params, max_days=300).start, date(2026, 1, 1))
        with self.assertRaisesMessage(ValueError, '自定义区间最长 50 天'):
            self._resolve(params, max_span_days=50)  # 旧参数名仍可用

    def test_future_end_opt_in(self):
        params = {'start': '2026-09-01', 'end': '2026-09-05'}
        self.assertEqual(self._resolve(params).end, date(2026, 9, 5))  # 缺省保持旧行为
        with self.assertRaisesMessage(ValueError, 'end 不能晚于今天'):
            self._resolve(params, allow_future_end=False)
        self.assertEqual(self._resolve({'end': '2026-09-04'}, allow_future_end=False).end, self.TODAY)

    def test_custom_default_days_and_messages(self):
        w = self._resolve({'end': '2026-09-04'}, custom_default_days=90)
        self.assertEqual(w.start, date(2026, 6, 6))
        with self.assertRaisesMessage(ValueError, 'start/end 必须是 YYYY-MM-DD'):
            self._resolve({'start': '2026/01/01'}, messages={'bad_date': 'start/end 必须是 YYYY-MM-DD'})
        with self.assertRaisesMessage(ValueError, 'period 仅支持 1w/1m/ytd'):
            self._resolve({'period': '5y'})


class ErrorHierarchyTests(SimpleTestCase):
    def test_hierarchy_and_legacy_paths(self):
        from . import errors
        from .common import upstream
        from . import views

        self.assertTrue(issubclass(errors.BadRequest, errors.StockTraceError))
        self.assertTrue(issubclass(errors.BadRequest, ValueError))  # 旧 except ValueError 仍能接住
        for cls in (errors.UpstreamTimeout, errors.UpstreamCircuitOpen):
            self.assertTrue(issubclass(cls, errors.UpstreamError))
            self.assertFalse(issubclass(cls, ValueError))
        # 旧导入路径与新定义是同一个类
        self.assertIs(upstream.UpstreamTimeout, errors.UpstreamTimeout)
        self.assertIs(views.UpstreamCircuitOpen, errors.UpstreamCircuitOpen)
        self.assertEqual(
            [errors.BadRequest.status_code, errors.UpstreamError.status_code,
             errors.UpstreamTimeout.status_code, errors.UpstreamCircuitOpen.status_code],
            [400, 502, 504, 503],
        )
