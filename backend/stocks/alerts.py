"""价格提醒评估：收盘汇总与盘中任务顺带执行，无独立轮询线程。

数据来源（全部真实落库数据，缺失即跳过，不当 0 处理）：
- 价格类规则：最新分钟 bar 收盘价（盘中实时性）→ 无分钟数据时退回最新日收盘价；
- 涨跌幅类规则：最新日线的 change_pct（相对昨收）。

去重：同一规则同一交易日只产生一条 AlertEvent。
可选推送：settings 里配 STOCKTRACE_PUSH_URL（Server酱兼容，POST {title, desp}）。
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.utils import timezone

from .models import AlertEvent, PriceAlert

logger = logging.getLogger(__name__)


def _latest_daily(stock):
    return stock.daily_quotes.order_by('-trade_date').first()


def _hit(alert, price, daily):
    threshold = float(alert.threshold)
    if alert.rule_type == PriceAlert.PRICE_ABOVE:
        return price is not None and price >= threshold
    if alert.rule_type == PriceAlert.PRICE_BELOW:
        return price is not None and price <= threshold
    if alert.rule_type == PriceAlert.DAILY_PCT_ABOVE:
        return daily is not None and daily.change_pct is not None \
            and float(daily.change_pct) >= threshold
    if alert.rule_type == PriceAlert.DAILY_PCT_BELOW:
        return daily is not None and daily.change_pct is not None \
            and float(daily.change_pct) <= -abs(threshold)
    return False


def _current_price(stock):
    """最新分钟收盘价（当日）；没有分钟数据退回最新日收盘价。"""
    bar = stock.minute_bars.order_by('-datetime').first()
    if bar is not None:
        return float(bar.close)
    daily = _latest_daily(stock)
    return float(daily.close_price) if daily is not None else None


def _message(alert, price):
    stock = alert.stock
    name = stock.name or stock.code
    if alert.rule_type in (PriceAlert.PRICE_ABOVE, PriceAlert.PRICE_BELOW):
        desc = '现价' if price is not None else '收盘价'
        return (
            f'{name} {desc} {price:.2f} 已{"上穿" if alert.rule_type == PriceAlert.PRICE_ABOVE else "下穿"}'
            f' {alert.threshold}'
        )
    if alert.rule_type == PriceAlert.DAILY_PCT_ABOVE:
        return f'{name} 日涨幅已达 {alert.threshold}%'
    return f'{name} 日跌幅已达 {alert.threshold}%'


def push_message(title, desp):
    """可选外发推送（Server酱兼容，POST {title, desp}）；未配置或失败都静默。"""
    url = getattr(settings, 'STOCKTRACE_PUSH_URL', '') or ''
    if not url or not desp:
        return False
    try:
        requests.post(url, json={'title': title, 'desp': desp}, timeout=5).raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"推送失败（不影响主流程）: {e}")
        return False


def _push(events):
    """可选外发推送；未配置或失败都不影响主流程（触发记录已在本地）。"""
    if not events:
        return
    push_message('StockTrace 价格提醒', '\n'.join(event.message for event in events))


def _evaluate_one(alert, today):
    """评估单条规则；命中则落库并返回 AlertEvent，否则返回 None。"""
    stock = alert.stock
    price = _current_price(stock) \
        if alert.rule_type in (PriceAlert.PRICE_ABOVE, PriceAlert.PRICE_BELOW) else None
    daily = _latest_daily(stock) \
        if alert.rule_type in (PriceAlert.DAILY_PCT_ABOVE, PriceAlert.DAILY_PCT_BELOW) else None
    if not _hit(alert, price, daily):
        return None
    event = AlertEvent.objects.create(
        alert=alert, stock=stock,
        message=_message(alert, price), trade_date=today,
    )
    alert.last_triggered_at = timezone.now()
    alert.save(update_fields=['last_triggered_at'])
    return event


def evaluate_alerts():
    """评估全部启用规则，返回本次新触发的 AlertEvent 列表。"""
    from .services import is_trading_day

    if not is_trading_day():
        return []

    today = timezone.localdate()
    already = set(
        AlertEvent.objects.filter(trade_date=today).values_list('alert_id', flat=True)
    )
    new_events = []
    for alert in PriceAlert.objects.filter(is_active=True).select_related('stock'):
        if alert.id in already:
            continue
        try:
            event = _evaluate_one(alert, today)
        except Exception:
            logger.exception(f"价格提醒 #{alert.id} 评估失败（跳过该条，继续其余规则）")
            continue
        if event is not None:
            new_events.append(event)

    _push(new_events)
    if new_events:
        logger.info(f"价格提醒触发 {len(new_events)} 条: {[e.message for e in new_events]}")
    return new_events
