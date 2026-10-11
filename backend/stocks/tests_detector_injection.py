"""探测器注入点（周期计数 / 交易日历）的回归测试：缺省行为不变，注入后不查全局日历与库。

注入点服务于离线回测（stocks.detector.backtest），这里用最小的日历替身验证接口契约。
"""

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from .detector import review, signals


def _weekdays(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


class _SetCalendar:
    """三态日历替身：覆盖范围内按集合判定，范围外未知（None）。"""

    def __init__(self, days):
        self.days = set(days)
        self.first, self.last = min(days), max(days)

    def is_trading_day(self, day=None):
        if day is None or day < self.first or day > self.last:
            return None
        return day in self.days


def _no_global_calendar(*_a, **_k):
    raise AssertionError('注入日历后不应再查全局交易日历')


# ============================================================
# 注入点：默认行为不变，注入后不再查全局日历/数据库
# ============================================================

class CalendarInjectionTests(SimpleTestCase):
    def setUp(self):
        self.days = _weekdays(date(2024, 1, 1), 60)
        self.cal = _SetCalendar(self.days)

    def test_previous_trading_days_uses_injected_calendar(self):
        with patch('stocks.trading_calendar.is_trading_day', side_effect=_no_global_calendar):
            got = signals._previous_trading_days(self.days[10], 5, calendar=self.cal)
        self.assertEqual(got, self.days[5:10][::-1])

    def test_previous_trading_days_unknown_outside_calendar(self):
        # 日历起点之前是「未知」，按 ALG-15 返回 None，不按自然日推进
        self.assertIsNone(signals._previous_trading_days(self.days[2], 5, calendar=self.cal))

    def test_next_trading_days_and_evaluate_bars_use_injected_calendar(self):
        with patch('stocks.trading_calendar.is_trading_day', side_effect=_no_global_calendar):
            self.assertEqual(review._next_trading_days(self.days[0], 3, calendar=self.cal), self.days[1:4])
            before = [(d, 10.0, 10.0) for d in self.days[:30]]
            after = [(d, 10.0, 10.0 * (1.02 ** (k + 1))) for k, d in enumerate(self.days[30:35])]
            out = review._evaluate_bars(SimpleNamespace(trade_date=self.days[29], direction='up'),
                                        before, after, base_is_open=True, calendar=self.cal)
        self.assertIsNotNone(out)
        self.assertTrue(out['hit'])

    def test_evaluate_bars_default_path_keeps_old_signature(self):
        # 不注入时仍按旧签名调用 _next_trading_days（旧测试替身不必认识 calendar）
        calls = []

        def fake_next(after, n):
            calls.append((after, n))
            return None

        with patch.object(review, '_next_trading_days', fake_next):
            self.assertIsNone(review._evaluate_bars(
                SimpleNamespace(trade_date=self.days[0], direction='up'),
                [(self.days[0], 1.0, 1.0)], [], base_is_open=True))
        self.assertEqual(calls, [(self.days[0], review.REVIEW_LOOKBACK)])


def _vote_cfg(key, direction='up'):
    def ev(ctx, params):
        return {'available': True, 'value': 30.0 if direction == 'up' else -30.0,
                'direction': direction, 'note': '', 'series': []}
    return {'key': key, 'name': key, 'weight': 1.0, 'params': {}, 'targets': ('stock',),
            'evaluate': ev, 'enabled': True}


class EvaluateTargetInjectionTests(SimpleTestCase):
    ctx = {'kind': 'stock', 'code': 'sh600000', 'as_of': '2024-03-05'}

    def test_cycle_counter_replaces_db_count(self):
        seen = []

        def counter(kind, code, direction, before):
            seen.append((kind, code, direction, before))
            return 2

        with patch.object(signals, '_count_cycles', side_effect=AssertionError('不应查库')):
            sd, _dims = signals.evaluate_target(self.ctx, [_vote_cfg('a'), _vote_cfg('b')], cycle_counter=counter)
        self.assertEqual(sd['cycles'], 3)
        self.assertEqual(sd['status'], 'active')
        self.assertEqual(seen, [('stock', 'sh600000', 'up', date(2024, 3, 5))])

    def test_default_path_calls_count_cycles_with_old_signature(self):
        with patch.object(signals, '_count_cycles', return_value=None) as m:
            sd, _dims = signals.evaluate_target(self.ctx, [_vote_cfg('a'), _vote_cfg('b')])
        m.assert_called_once_with('stock', 'sh600000', 'up', date(2024, 3, 5))
        self.assertTrue(sd['cycles_unknown'])
        self.assertEqual(sd['cycles'], 1)

    def test_calendar_forwarded_to_default_count_cycles(self):
        cal = _SetCalendar(_weekdays(date(2024, 1, 1), 60))
        with patch.object(signals, '_count_cycles', return_value=0) as m:
            signals.evaluate_target(self.ctx, [_vote_cfg('a'), _vote_cfg('b')], calendar=cal)
        self.assertIs(m.call_args.kwargs['calendar'], cal)


class InjectedCalendarDbTests(TestCase):
    def test_count_cycles_with_injected_calendar(self):
        from .models import DetectorSignal

        days = _weekdays(date(2024, 1, 1), 60)
        cal = _SetCalendar(days)
        for d in days[37:40]:
            DetectorSignal.objects.create(target_kind='stock', target_code='600000', target_name='x',
                                          trade_date=d, direction='up')
        with patch('stocks.trading_calendar.is_trading_day', side_effect=_no_global_calendar):
            self.assertEqual(signals._count_cycles('stock', '600000', 'up', days[40], calendar=cal), 3)
            self.assertEqual(signals._count_cycles('stock', '600000', 'down', days[40], calendar=cal), 0)

    def test_review_pending_with_injected_calendar(self):
        days = _weekdays(date(2024, 1, 1), 60)
        cal = _SetCalendar(days)
        with patch('stocks.detector.review.recent_trading_days', side_effect=_no_global_calendar):
            summary = review.review_pending(days[30], {}, calendar=cal)
            self.assertEqual(summary['reviewed'], 0)
            self.assertNotIn('note', summary)
            # 日历覆盖不到 → 如实跳过回看
            unknown = review.review_pending(days[2], {}, calendar=cal)
        self.assertIn('交易日历不可用', unknown['note'])

    def test_run_scan_guard_uses_injected_calendar(self):
        from .detector import scan

        cal = _SetCalendar(_weekdays(date(2024, 1, 1), 30))
        with patch('stocks.trading_calendar.is_trading_day', side_effect=_no_global_calendar):
            out = scan.run_scan(date(2024, 1, 6), calendar=cal)  # 周六
        self.assertEqual(out['skipped'], 'non_trading_day')
