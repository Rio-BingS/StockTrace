"""P0 回归测试（提醒与探测器方向）：ALG-02 / DAT-08 / ALG-01 / ALG-03。

约定与 tests_features 一致：交易日历 patch 为「每天都交易日」，测试绝不触网。
"""

from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from . import alerts
from .models import AlertEvent, DailyQuote, MinuteBar, PriceAlert, Stock

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
        with patch('stocks.services.is_trading_day', _trading_true):
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

        with patch('stocks.services.is_trading_day', _trading_true), \
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
        self.price_alert = PriceAlert.objects.create(
            stock=self.stock, rule_type=PriceAlert.PRICE_ABOVE, threshold=Decimal('10.5'),
        )

    def _eval(self, now):
        with patch('stocks.services.is_trading_day', _trading_true):
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
        # 当日最新价 10.70 / 昨收 10.60 = +0.94%，不应因昨日 change_pct=6 触发
        _make_bar(self.stock, datetime.combine(_TODAY, time(10, 0)), Decimal('10.70'))
        events = self._eval(_at(10, 5))
        self.assertEqual([e.alert_id for e in events], [self.price_alert.id])

    def test_after_close_uses_today_daily_change_pct(self):
        _make_daily(self.stock, _TODAY, Decimal('11.20'), 5.66)
        events = self._eval(_at(15, 10))
        self.assertEqual({e.alert_id for e in events}, {self.pct_alert.id, self.price_alert.id})
        price_event = next(e for e in events if e.alert_id == self.price_alert.id)
        self.assertIn('收盘价 11.20', price_event.message)

    def test_non_trading_day_skips(self):
        _make_bar(self.stock, datetime.combine(_TODAY, time(10, 0)), Decimal('11.30'))
        with patch('stocks.services.is_trading_day', return_value=False):
            self.assertEqual(alerts.evaluate_alerts(now=_at(10, 5)), [])
