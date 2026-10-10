"""P0 回归测试（提醒与探测器方向）：ALG-02 / DAT-08 / ALG-01 / ALG-03。

约定与 tests_features 一致：交易日历 patch 为「每天都交易日」，测试绝不触网。
"""

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from . import alerts
from .models import AlertEvent, DailyQuote, MinuteBar, PriceAlert, Stock

_TODAY = timezone.localdate()


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
            events = alerts.evaluate_alerts()
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
            events = alerts.evaluate_alerts()
        self.assertEqual([e.stock_id for e in events], [self.other.id])
        self.assertTrue(any('评估失败' in line for line in logs.output))
        self.assertFalse(AlertEvent.objects.filter(alert=bad).exists())
