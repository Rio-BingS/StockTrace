"""P2 tests-arch 方向回归：主键类型钉住（ARC-08）、交易日历独立模块（ARC-02）。"""

from django.apps import apps
from django.conf import settings
from django.db import models
from django.test import SimpleTestCase


class DefaultAutoFieldTests(SimpleTestCase):
    """ARC-08：DEFAULT_AUTO_FIELD 显式为 BigAutoField，与现有迁移一致。"""

    def test_setting_is_big_auto_field(self):
        self.assertEqual(settings.DEFAULT_AUTO_FIELD, 'django.db.models.BigAutoField')

    def test_all_stocks_models_use_big_auto_field(self):
        for model in apps.get_app_config('stocks').get_models():
            self.assertIsInstance(model._meta.pk, models.BigAutoField, model.__name__)


# ============================================================
# ARC-02：交易日历独立模块
# ============================================================

import ast  # noqa: E402
from datetime import date, datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest.mock import patch  # noqa: E402

from django.utils import timezone  # noqa: E402

_STOCKS_DIR = Path(__file__).resolve().parent

# 2026-10：1-8 国庆休市，9 日（周五）起开市；10/11 为周末
_HOLIDAYS = {date(2026, 10, d) for d in range(1, 9)}


def _fake_is_trading_day(d=None):
    return d.weekday() < 5 and d not in _HOLIDAYS


def _imported_modules(path):
    """返回文件里 import 的模块（相对导入还原成 stocks 下的绝对名）"""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    rel_pkg = path.parent.relative_to(_STOCKS_DIR.parent).parts
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = list(rel_pkg[:len(rel_pkg) - node.level + 1]) if node.level else []
            mod = '.'.join(base + ([node.module] if node.module else []))
            out.add(mod)
            out.update(f'{mod}.{a.name}' for a in node.names)
    return out


class CalendarDependencyTests(SimpleTestCase):
    """依赖方向：日历模块不依赖业务模块；market 包不再反向依赖 services。"""

    def test_trading_calendar_has_no_business_imports(self):
        mods = _imported_modules(_STOCKS_DIR / 'trading_calendar.py')
        bad = {m for m in mods if m.startswith(('stocks.services', 'stocks.market', 'stocks.detector'))}
        self.assertEqual(bad, set())

    def test_market_package_does_not_import_services(self):
        for path in sorted((_STOCKS_DIR / 'market').glob('*.py')):
            mods = _imported_modules(path)
            self.assertFalse(any(m.startswith('stocks.services') for m in mods), path.name)

    def test_detector_uses_public_calendar_api(self):
        for name in ('views.py', 'review.py'):
            mods = _imported_modules(_STOCKS_DIR / 'detector' / name)
            self.assertFalse(
                any(m.startswith('stocks.market.snapshots._') for m in mods), name)


@patch('stocks.trading_calendar.is_trading_day', side_effect=_fake_is_trading_day)
class CalendarFunctionTests(SimpleTestCase):
    def test_recent_trading_days_skips_holidays(self, _m):
        from stocks import trading_calendar as cal
        self.assertEqual(
            cal.recent_trading_days(3, end_date=date(2026, 10, 12)),
            [date(2026, 9, 30), date(2026, 10, 9), date(2026, 10, 12)])

    def test_recent_trading_days_none_when_calendar_unknown(self, m):
        from stocks import trading_calendar as cal
        m.side_effect = lambda d=None: None
        self.assertIsNone(cal.recent_trading_days(2, end_date=date(2026, 10, 12)))

    def test_expected_latest_date_respects_1530(self, _m):
        from stocks import trading_calendar as cal
        tz = timezone.get_default_timezone()
        before = timezone.make_aware(datetime(2026, 10, 12, 15, 29), tz)
        after = timezone.make_aware(datetime(2026, 10, 12, 15, 31), tz)
        with patch('django.utils.timezone.localtime', return_value=before):
            self.assertEqual(cal.expected_latest_date(), date(2026, 10, 9))
        with patch('django.utils.timezone.localtime', return_value=after):
            self.assertEqual(cal.expected_latest_date(), date(2026, 10, 12))

    def test_expected_latest_date_none_on_calendar_error(self, m):
        from stocks import trading_calendar as cal
        m.side_effect = RuntimeError('down')
        self.assertIsNone(cal.expected_latest_date())

    def test_prev_trading_day_and_candidates(self, _m):
        from stocks import trading_calendar as cal
        self.assertEqual(cal.prev_trading_day(date(2026, 10, 9)), date(2026, 9, 30))
        # 今天是周日：先退到周六（仍非交易日则逐日跳过）
        self.assertEqual(
            cal.recent_day_candidates(2, today=date(2026, 10, 11)),
            [date(2026, 10, 9), date(2026, 9, 30)])

    def test_last_session_close_memo_and_holiday(self, m):
        from stocks import trading_calendar as cal
        tz = timezone.get_default_timezone()
        now = timezone.make_aware(datetime(2026, 10, 9, 10, 0), tz)  # 节后首日盘中
        with patch('django.utils.timezone.localtime', return_value=now):
            close = cal.last_session_close()
            self.assertEqual(close, timezone.make_aware(datetime(2026, 9, 30, 15, 0), tz))
            calls = m.call_count
            self.assertEqual(cal.last_session_close(), close)  # 60 秒记忆，不再查日历
            self.assertEqual(m.call_count, calls)

    def test_data_changing_window(self, _m):
        from stocks import trading_calendar as cal
        tz = timezone.get_default_timezone()
        self.assertTrue(cal.data_changing_now(timezone.make_aware(datetime(2026, 10, 9, 20, 0), tz)))
        self.assertFalse(cal.data_changing_now(timezone.make_aware(datetime(2026, 10, 9, 21, 30), tz)))
        self.assertFalse(cal.data_changing_now(timezone.make_aware(datetime(2026, 10, 10, 10, 0), tz)))


@patch('stocks.trading_calendar.is_trading_day', side_effect=_fake_is_trading_day)
class LegacyWrapperTests(SimpleTestCase):
    """旧位置的薄包装与新模块结果一致（合并期间其他方向仍可能调用旧名）。"""

    def test_wrappers_delegate(self, _m):
        import importlib

        from stocks import services, trading_calendar as cal
        from stocks.market import sentiment, snapshots

        # stocks.market 包把 _cache 字典再导出成同名属性，需按模块名取
        _cache = importlib.import_module('stocks.market._cache')
        d = date(2026, 10, 12)
        self.assertTrue(services.is_trading_day(d))
        self.assertFalse(services.is_trading_day(date(2026, 10, 1)))
        self.assertEqual(snapshots._recent_trading_days(2, end_date=d), cal.recent_trading_days(2, end_date=d))
        self.assertEqual(sentiment._prev_trading_day(d), date(2026, 10, 9))
        self.assertEqual(sentiment._recent_day_candidates(1, today=d), [d])
        now = timezone.make_aware(datetime(2026, 10, 12, 10, 0), timezone.get_default_timezone())
        self.assertTrue(_cache._data_changing_now(now))
        with patch('django.utils.timezone.localtime', return_value=now):
            self.assertEqual(snapshots._expected_latest_date(), date(2026, 10, 9))
            self.assertEqual(_cache._last_session_close().date(), date(2026, 10, 9))

    def test_is_trading_time_wrapper(self, _m):
        from stocks import services
        tz = timezone.get_default_timezone()
        self.assertTrue(services.is_trading_time(timezone.make_aware(datetime(2026, 10, 12, 9, 30), tz)))
        self.assertFalse(services.is_trading_time(timezone.make_aware(datetime(2026, 10, 12, 12, 0), tz)))

    def test_reset_wrapper_clears_calendar_state(self, _m):
        from stocks import services, trading_calendar as cal
        cal._trade_day_cache[date(2027, 1, 4)] = (True, 9e18)
        cal._calendar_fail_until['sina'] = 9e18
        services._reset_trade_calendar_cache()
        self.assertEqual(cal._trade_day_cache, {})
        self.assertEqual(cal._calendar_fail_until['sina'], 0)


class CalendarSwitchTests(SimpleTestCase):
    """市场缓存的日历开关走 settings，不再嗅探 sys.argv（ARC-02）。"""

    def test_runner_disables_calendar(self):
        from stocks.market._cache import _calendar_enabled
        self.assertFalse(settings.STOCKTRACE_MARKET_CALENDAR_ENABLED)
        self.assertFalse(_calendar_enabled())

    def test_switch_follows_settings_not_argv(self):
        from django.test import override_settings

        from stocks.market._cache import _calendar_enabled
        with override_settings(STOCKTRACE_MARKET_CALENDAR_ENABLED=True), \
                patch('sys.argv', ['manage.py', 'test']):
            self.assertTrue(_calendar_enabled())

    def test_cache_module_does_not_read_argv(self):
        self.assertNotIn('sys', _imported_modules(_STOCKS_DIR / 'market' / '_cache.py'))
