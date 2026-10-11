"""P0 回归测试（提醒与探测器方向）：ALG-02 / DAT-08 / ALG-01 / ALG-03。

约定与 tests_features 一致：交易日历 patch 为「每天都交易日」，测试绝不触网。
"""

from datetime import datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import alerts
from .detector import calm, scan
from .models import (
    AlertEvent,
    DailyQuote,
    DetectorRun,
    DetectorSignal,
    MinuteBar,
    PriceAlert,
    Stock,
)

_TODAY = timezone.localdate()
_YESTERDAY = _TODAY - timedelta(days=1)


def _at(hh, mm, day=None):
    """本地时区的某个时刻（evaluate_alerts 的 now 注入）。"""
    return timezone.make_aware(datetime.combine(day or _TODAY, time(hh, mm)))


_INTRADAY = _at(10, 30)


def _trading_true(day=None):
    return True


def _make_daily(stock, day, close, change_pct=None):
    return DailyQuote.objects.create(
        stock=stock, trade_date=day,
        open_price=close, close_price=close, high_price=close, low_price=close,
        open_close_diff=Decimal('0'), open_close_pct=Decimal('0'),
        high_low_diff=Decimal('0'), high_low_pct=Decimal('0'),
        change_pct=None if change_pct is None else Decimal(str(change_pct)),
    )


def _make_bar(stock, dt, close):
    return MinuteBar.objects.create(
        stock=stock, datetime=timezone.make_aware(dt),
        open=Decimal(str(close)), close=Decimal(str(close)), high=Decimal(str(close)),
        low=Decimal(str(close)), volume=100,
    )


class AlertDailyFallbackTests(TestCase):
    """ALG-02：无分钟数据时退回日线收盘价（字段是 close_price，不是 close）；单条异常不中断本轮。"""

    def setUp(self):
        self.stock = Stock.objects.create(code='600000', name='浦发银行')
        self.other = Stock.objects.create(code='600036', name='招商银行')

    def test_price_rule_falls_back_to_daily_close_price_without_minute_bars(self):
        _make_daily(self.stock, _TODAY, Decimal('10.50'), 1.0)
        PriceAlert.objects.create(
            stock=self.stock, rule_type=PriceAlert.PRICE_ABOVE, threshold=Decimal('10'),
        )
        with patch('stocks.trading_calendar.is_trading_day', _trading_true):
            events = alerts.evaluate_alerts(now=_INTRADAY)
        self.assertEqual(len(events), 1)
        self.assertIn('10.50', events[0].message)

    def test_single_alert_failure_does_not_abort_round(self):
        bad = PriceAlert.objects.create(
            stock=self.stock, rule_type=PriceAlert.PRICE_ABOVE, threshold=Decimal('1'),
        )
        _make_daily(self.other, _TODAY, Decimal('20'), 1.0)
        PriceAlert.objects.create(
            stock=self.other, rule_type=PriceAlert.PRICE_ABOVE, threshold=Decimal('1'),
        )
        real = alerts._evaluate_one

        def flaky(alert, today):
            if alert.id == bad.id:
                raise RuntimeError('boom')
            return real(alert, today)

        with patch('stocks.trading_calendar.is_trading_day', _trading_true), \
                patch.object(alerts, '_evaluate_one', side_effect=flaky), \
                self.assertLogs('stocks.alerts', level='ERROR') as logs:
            events = alerts.evaluate_alerts(now=_INTRADAY)
        self.assertEqual([e.stock_id for e in events], [self.other.id])
        self.assertTrue(any('评估失败' in line for line in logs.output))
        self.assertFalse(AlertEvent.objects.filter(alert=bad).exists())


class AlertStaleDataTests(TestCase):
    """DAT-08：只用当日数据评估；盘前/午休不评估；陈旧数据不占当日去重名额。"""

    def setUp(self):
        self.stock = Stock.objects.create(code='600000', name='浦发银行')
        # 昨日涨 6%，库里只有昨日日线与昨日最后一根分钟 bar
        _make_daily(self.stock, _YESTERDAY - timedelta(days=1), Decimal('10.00'), 0.0)
        _make_daily(self.stock, _YESTERDAY, Decimal('10.60'), 6.0)
        _make_bar(self.stock, datetime.combine(_YESTERDAY, time(15, 0)), Decimal('10.60'))
        self.pct_alert = PriceAlert.objects.create(
            stock=self.stock, rule_type=PriceAlert.DAILY_PCT_ABOVE, threshold=Decimal('5'),
        )
        # 阈值高于昨收 10.60：上穿语义下今天涨过 11 才算穿越（ALG-14）
        self.price_alert = PriceAlert.objects.create(
            stock=self.stock, rule_type=PriceAlert.PRICE_ABOVE, threshold=Decimal('11'),
        )

    def _eval(self, now):
        with patch('stocks.trading_calendar.is_trading_day', _trading_true):
            return alerts.evaluate_alerts(now=now)

    def test_premarket_does_not_evaluate(self):
        _make_bar(self.stock, datetime.combine(_TODAY, time(9, 25)), Decimal('11.30'))
        self.assertEqual(self._eval(_at(9, 0)), [])
        self.assertEqual(self._eval(_at(9, 25)), [])
        self.assertFalse(AlertEvent.objects.exists())

    def test_lunch_break_does_not_evaluate(self):
        _make_bar(self.stock, datetime.combine(_TODAY, time(11, 30)), Decimal('11.30'))
        self.assertEqual(self._eval(_at(12, 0)), [])
        self.assertFalse(AlertEvent.objects.exists())

    def test_stale_yesterday_data_never_triggers_today(self):
        # 盘中但当日尚无任何数据：昨日 6% 涨幅 / 昨日 bar 一律不用
        self.assertEqual(self._eval(_at(9, 35)), [])
        self.assertFalse(AlertEvent.objects.exists())

    def test_stale_data_does_not_consume_dedup_slot(self):
        self.assertEqual(self._eval(_at(9, 35)), [])
        # 当日真实上涨到 +6.6%（相对昨收 10.60）后才触发，且未被「当日已触发」挡住
        _make_bar(self.stock, datetime.combine(_TODAY, time(10, 0)), Decimal('11.30'))
        events = self._eval(_at(10, 5))
        self.assertEqual({e.alert_id for e in events}, {self.pct_alert.id, self.price_alert.id})
        self.assertTrue(all(e.trade_date == _TODAY for e in events))

    def test_intraday_pct_uses_today_bar_vs_prev_close(self):
        # 当日最新价 10.70 / 昨收 10.60 = +0.94%，不应因昨日 change_pct=6 触发；
        # 10.70 也没上穿 11，价格规则同样不触发
        _make_bar(self.stock, datetime.combine(_TODAY, time(10, 0)), Decimal('10.70'))
        events = self._eval(_at(10, 5))
        self.assertEqual(events, [])

    def test_after_close_uses_today_daily_change_pct(self):
        _make_daily(self.stock, _TODAY, Decimal('11.20'), 5.66)
        events = self._eval(_at(15, 10))
        self.assertEqual({e.alert_id for e in events}, {self.pct_alert.id, self.price_alert.id})
        price_event = next(e for e in events if e.alert_id == self.price_alert.id)
        self.assertIn('收盘价 11.20', price_event.message)

    def test_non_trading_day_skips(self):
        _make_bar(self.stock, datetime.combine(_TODAY, time(10, 0)), Decimal('11.30'))
        with patch('stocks.trading_calendar.is_trading_day', return_value=False):
            self.assertEqual(alerts.evaluate_alerts(now=_at(10, 5)), [])


def _after_close_clock():
    """只替换 scan 模块里的 timezone：把「现在」固定在今天 16:00，绕过 15:30 前的守卫。"""
    fixed = _at(16, 0)
    return SimpleNamespace(localdate=lambda *a: fixed.date(), localtime=lambda *a: fixed)


class CalmNoneHandlingTests(TestCase):
    """ALG-01：序列含 None 时平静判定返回 (False, reason)，不抛异常。"""

    def test_price_calm_with_mostly_none_recent_volumes(self):
        volumes = [1000.0 + (i % 3) * 10 for i in range(30)] + [None, None, None, None, 1000.0]
        closes = [10.0 + (i % 2) * 0.05 for i in range(35)]
        ctx = {
            'kind': 'stock', 'volume': volumes, 'close': closes,
            'high': [c * 1.01 for c in closes], 'low': [c * 0.99 for c in closes],
        }
        ok, meta = calm.is_calm(ctx)
        self.assertFalse(ok)
        self.assertIn('reason', meta)

    def test_sector_calm_when_every_rolling_window_has_none(self):
        # 行业回填后的头几天：change_pct 每隔几天就是 None，滚动窗口全不完整
        # （旧代码：roll_std 为空 → _percentile 返回 None → float > None 抛 TypeError）
        pcts = [None if i % 4 == 0 else 0.1 + 0.05 * (i % 3) for i in range(17)]
        ctx = {'kind': 'industry', 'net': [1e8 if i % 2 else -1e8 for i in range(len(pcts))],
               'change_pct': pcts}
        ok, meta = calm.is_calm(ctx)
        self.assertFalse(ok)
        self.assertIn(meta['reason'], ('涨跌幅窗口存在缺失快照', '涨跌幅滚动样本不足'))

    def test_sector_calm_with_none_in_current_window(self):
        pcts = [0.1, 0.2, 0.15, 0.12, 0.18] * 4 + [0.1, None, 0.2, 0.1, 0.1]
        ctx = {'kind': 'industry', 'net': [1e8 if i % 2 else -1e8 for i in range(len(pcts))],
               'change_pct': pcts}
        ok, meta = calm.is_calm(ctx)
        self.assertFalse(ok)
        self.assertEqual(meta['reason'], '涨跌幅窗口存在缺失快照')


class ScanTargetIsolationTests(TestCase):
    """ALG-01：单对象平静判定异常只降级自己，扫描继续并写 DetectorRun。"""

    def test_calm_exception_degrades_single_target(self):
        targets = [
            {'kind': 'stock', 'code': '000001', 'name': '坏对象'},
            {'kind': 'stock', 'code': '000002', 'name': '好对象'},
        ]

        def fake_calm(ctx, params=None):
            if ctx['code'] == '000001':
                raise TypeError("unsupported operand type(s) for /: 'NoneType' and 'float'")
            return False, {'reason': '不平静'}

        with patch('stocks.trading_calendar.is_trading_day', _trading_true), \
                patch.object(scan, 'timezone', _after_close_clock()), \
                patch.object(scan, 'save_margin_snapshot', return_value=0), \
                patch.object(scan, '_collect_targets', return_value=targets), \
                patch.object(scan, '_build_context',
                             side_effect=lambda t: ({'kind': t['kind'], 'code': t['code'],
                                                     'as_of': _TODAY.isoformat()}, None)), \
                patch.object(scan.calm_mod, 'is_calm', side_effect=fake_calm), \
                self.assertLogs('stocks.detector.scan', level='ERROR'):
            summary = scan.run_scan()
        self.assertEqual(summary['status'], 'partial')
        self.assertEqual(summary['targets_scanned'], 2)
        run = DetectorRun.objects.get(trade_date=_TODAY)
        reasons = {d['target']: d['reason'] for d in run.degraded}
        self.assertIn('stock:000001', reasons)
        self.assertIn('平静判定异常', reasons['stock:000001'])
        self.assertNotIn('stock:000002', reasons)


class DetectorSignalsAllDatesTests(TestCase):
    """ALG-03（额外修复）：all_dates=1 与筛选参数同用不再 500，且历史窗是近 30 天。"""

    def _signal(self, code, day, severity):
        return DetectorSignal.objects.create(
            target_kind='industry', target_code=code, target_name=code,
            trade_date=day, direction='up', cycles=1, score=40,
            severity=severity, status=DetectorSignal.STATUS_WATCHING, dimensions={},
        )

    def test_all_dates_with_filter_and_30_day_window(self):
        self._signal('半导体', _TODAY, DetectorSignal.SEVERITY_HIGH)
        self._signal('银行', _TODAY - timedelta(days=5), DetectorSignal.SEVERITY_LOW)
        self._signal('旧信号', _TODAY - timedelta(days=45), DetectorSignal.SEVERITY_HIGH)
        resp = self.client.get(reverse('detector-signals'), {'all_dates': '1', 'severity': 'high'})
        self.assertEqual(resp.status_code, 200)
        codes = [row['target_code'] for row in resp.json()['items']]
        self.assertEqual(codes, ['半导体'])
        resp = self.client.get(reverse('detector-signals'), {'all_dates': '1'})
        self.assertEqual(resp.json()['summary']['total'], 2)
