"""价格提醒评估：收盘汇总与盘中任务顺带执行，无独立轮询线程。

数据来源（全部真实落库数据，缺失即跳过，不当 0 处理；只认「当日」数据）：
- 价格类规则：当日最新分钟 bar 收盘价（盘中实时性）→ 无当日分钟数据时退回当日日线收盘价；
- 涨跌幅类规则：当日日线的 change_pct（收盘汇总后才有）→ 盘中没有当日日线时，
  用「当日最新分钟价 / 上一交易日收盘价」实时计算。

时段：非交易日、盘前（09:30 前）、午休（11:30~13:00）不评估——盘前库里只有昨天的
数据，拿昨日涨跌幅以今天的日期触发既是误报，又会占掉当日去重名额（P0 DAT-08）。
收盘后（15:00 以后）照常评估，供收盘汇总任务使用。

穿越语义（ALG-14）：「价格上穿/下穿」比较的是「上一交易日收盘在阈值哪一侧」与
「当日价格在哪一侧」——昨收在阈值下方、今天到达或超过阈值才算上穿（下穿对称）。
旧实现是水平判断（价格 ≥ 阈值），价格一直在阈值上方时每个交易日都重复触发。
不需要新增「上次状态」字段：上一交易日收盘就是上一次评估结束时的状态。
上一交易日收盘缺失（新加自选、无历史）时无法判断方向，退回水平判断，最多每天一次。
涨跌幅规则两侧对称：阈值一律取绝对值（涨幅 ≥ |x|、跌幅 ≤ -|x|）。

去重：同一规则同一交易日只产生一条 AlertEvent；陈旧数据根本不会被评估，不占名额。
可选推送：settings 里配 STOCKTRACE_PUSH_URL（Server酱兼容，POST {title, desp}）。
"""

from __future__ import annotations

import logging
from datetime import time as dt_time

import requests
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import AlertEvent, PriceAlert

logger = logging.getLogger(__name__)


# 允许评估的时段：开盘后，且不在午休。收盘后放行（收盘汇总要用当日日线评估）。
_MORNING_OPEN = dt_time(9, 30)
_MORNING_CLOSE = dt_time(11, 30)
_AFTERNOON_OPEN = dt_time(13, 0)


def _in_alert_window(now):
    """盘前（09:30 前）与午休（11:30 后、13:00 前）返回 False。"""
    t = now.time()
    if t < _MORNING_OPEN:
        return False
    if _MORNING_CLOSE < t < _AFTERNOON_OPEN:
        return False
    return True


def _today_bar(stock, today):
    return stock.minute_bars.filter(datetime__date=today).order_by('-datetime').first()


def _today_daily(stock, today):
    return stock.daily_quotes.filter(trade_date=today).first()


def _prev_close(stock, today):
    prev = stock.daily_quotes.filter(trade_date__lt=today).order_by('-trade_date').first()
    return float(prev.close_price) if prev is not None and prev.close_price else None


def _current_price(stock, today):
    """当日最新分钟收盘价；没有当日分钟数据退回当日日收盘价；都没有返回 (None, None)。

    返回 (price, 来源描述)。旧日期的 bar / 日线一律不用（P0 DAT-08）。
    """
    bar = _today_bar(stock, today)
    if bar is not None:
        return float(bar.close), '现价'
    daily = _today_daily(stock, today)
    if daily is not None:
        return float(daily.close_price), '收盘价'
    return None, None


def _current_pct(stock, today):
    """当日涨跌幅（%）：优先当日日线 change_pct；盘中无当日日线时用当日最新分钟价 / 昨收实时计算。"""
    daily = _today_daily(stock, today)
    if daily is not None and daily.change_pct is not None:
        return float(daily.change_pct)
    bar = _today_bar(stock, today)
    prev_close = _prev_close(stock, today)
    if bar is None or not prev_close:
        return None
    return (float(bar.close) - prev_close) / prev_close * 100


def _hit(alert, price, pct, prev_close=None):
    """规则是否命中。prev_close 是上一交易日收盘（价格穿越规则用，缺失则按水平判断）。"""
    threshold = float(alert.threshold)
    if alert.rule_type == PriceAlert.PRICE_ABOVE:
        if price is None or price < threshold:
            return False
        return prev_close is None or prev_close < threshold
    if alert.rule_type == PriceAlert.PRICE_BELOW:
        if price is None or price > threshold:
            return False
        return prev_close is None or prev_close > threshold
    if alert.rule_type == PriceAlert.DAILY_PCT_ABOVE:
        return pct is not None and pct >= abs(threshold)
    if alert.rule_type == PriceAlert.DAILY_PCT_BELOW:
        return pct is not None and pct <= -abs(threshold)
    return False


def _message(alert, price, desc='现价'):
    stock = alert.stock
    name = stock.name or stock.code
    if alert.rule_type in (PriceAlert.PRICE_ABOVE, PriceAlert.PRICE_BELOW):
        return (
            f'{name} {desc} {price:.2f} 已{"上穿" if alert.rule_type == PriceAlert.PRICE_ABOVE else "下穿"}'
            f' {alert.threshold}'
        )
    if alert.rule_type == PriceAlert.DAILY_PCT_ABOVE:
        return f'{name} 日涨幅已达 {abs(alert.threshold)}%'
    return f'{name} 日跌幅已达 {abs(alert.threshold)}%'


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
    price = pct = desc = prev_close = None
    if alert.rule_type in (PriceAlert.PRICE_ABOVE, PriceAlert.PRICE_BELOW):
        price, desc = _current_price(stock, today)
        if price is not None:
            prev_close = _prev_close(stock, today)
    elif alert.rule_type in (PriceAlert.DAILY_PCT_ABOVE, PriceAlert.DAILY_PCT_BELOW):
        pct = _current_pct(stock, today)
    if not _hit(alert, price, pct, prev_close):
        return None
    # (alert, trade_date) 有唯一约束：并发评估时另一方已写入则视为已触发（PERF-06）
    try:
        with transaction.atomic():
            event, created = AlertEvent.objects.get_or_create(
                alert=alert, trade_date=today,
                defaults={'stock': stock, 'message': _message(alert, price, desc)},
            )
    except IntegrityError:
        return None
    if not created:
        return None
    alert.last_triggered_at = timezone.now()
    alert.save(update_fields=['last_triggered_at'])
    return event


def evaluate_alerts(now=None):
    """评估全部启用规则，返回本次新触发的 AlertEvent 列表。

    now：评估时刻（本地时区，测试注入用）；缺省取当前时间。
    非交易日 / 盘前 / 午休直接返回 []；单条规则异常只记日志，不中断本轮其余规则。
    """
    from .trading_calendar import is_trading_day

    now = timezone.localtime(now) if now is not None else timezone.localtime()
    today = now.date()
    if not is_trading_day(today):
        return []
    if not _in_alert_window(now):
        return []

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
