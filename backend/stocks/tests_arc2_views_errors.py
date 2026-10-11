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
