"""复盘节点（det-v2）：信号后 20 日与 60 日两个节点的相对沪深300 超额，前 10 日不判定失败。

为什么在 5 日障碍判定（review.py）之外再加两个节点：
- 第六轮（round6_horizon）：信号的超额兑现大体线性，前 10 日只兑现约 13%、增量
  t<1，20–40 日走平、40–60 日补涨；v1.8.0 向下信号 20/60 日相对全池 +1.29/+3.63pp。
  用 5 日结果判「失败」会系统性误判，所以页面以 20/60 日为准，前 10 日只显示
  「观察期」；
- 节点不落库、不参与调参：详情页按需从本地日线（个股）或上游日线（指数）现算，
  口径与 review 一致——基期 T+1 开盘（板块无开盘价，用 T+1 收盘），超额 = 标的
  收益 − 同区间沪深300 收益，按信号方向取号（向下信号跑输为正）。
"""

from __future__ import annotations

import logging
from datetime import date as _date

from .constants import BENCHMARK_CODE, CONTEXT_BARS

logger = logging.getLogger(__name__)

NODE_HORIZONS = (20, 60)
# 前 10 个交易日不判定失败（第六轮：前 10 日增量与 0 分不开）
NO_FAIL_DAYS = 10


def _ret(base, end):
    if not base or end is None:
        return None
    return (end / base - 1) * 100


def judge_nodes(after, direction, bench=None, base_is_open=True):
    """after：信号日 T 之后的 [(date, open, close)] 升序；bench：{date: (open, close)}。

    返回 {'elapsed', 'no_fail_days', 'nodes': [...]}；每个节点：
    - state='observe'：已过交易日 < NO_FAIL_DAYS，只给当前进度，不判定；
    - state='pending'：未到节点，给当前进度，不判定；
    - state='done'：到达节点，verdict = 'pass'（按方向超额 > 0）/'fail'。
    """
    off = 0 if base_is_open else 1
    span = [b for b in after if b[2] is not None]
    if len(span) <= off or (base_is_open and not span[0][1]):
        return {'elapsed': 0, 'no_fail_days': NO_FAIL_DAYS, 'nodes': [
            {'horizon': h, 'state': 'observe', 'days': 0} for h in NODE_HORIZONS]}
    base_date = span[0][0]
    base = span[0][1] if base_is_open else span[0][2]
    window = span[off:]
    elapsed = len(window)
    sign = -1 if direction == 'down' else 1
    nodes = []
    for h in NODE_HORIZONS:
        end = window[min(h, elapsed) - 1]
        fwd = _ret(base, end[2])
        bench_ret = None
        if bench:
            start_bar, end_bar = bench.get(base_date), bench.get(end[0])
            if start_bar and end_bar:
                bench_ret = _ret(start_bar[0] if base_is_open else start_bar[1], end_bar[1])
        excess = fwd - bench_ret if fwd is not None and bench_ret is not None else None
        node = {
            'horizon': h,
            'days': min(h, elapsed),
            'end_date': str(end[0]),
            'fwd_return': round(fwd, 2) if fwd is not None else None,
            'bench_return': round(bench_ret, 2) if bench_ret is not None else None,
            'excess': round(excess, 2) if excess is not None else None,
            'dir_excess': round(sign * excess, 2) if excess is not None else None,
            'verdict': None,
        }
        if elapsed >= h:
            node['state'] = 'done'
            if excess is not None:
                node['verdict'] = 'pass' if sign * excess > 0 else 'fail'
        elif elapsed < NO_FAIL_DAYS:
            node['state'] = 'observe'
        else:
            node['state'] = 'pending'
        nodes.append(node)
    return {'elapsed': elapsed, 'no_fail_days': NO_FAIL_DAYS, 'nodes': nodes}


def _index_bars(code):
    from ..market import indices

    data = indices.fetch_index_daily_series(code, days=CONTEXT_BARS)
    if not data.get('available'):
        return []
    return [(_date.fromisoformat(it['date']), it.get('open'), it.get('close')) for it in data['items']]


def _benchmark():
    try:
        return {d: (o, c) for d, o, c in _index_bars(BENCHMARK_CODE)}
    except Exception:  # noqa: BLE001 上游边界：基准取不到时超额留空
        logger.warning("复盘节点取基准日线失败", exc_info=True)
        return {}


def _stock_after(signal):
    from ..models import DailyQuote, Stock

    stock = Stock.objects.filter(code=signal.target_code).first()
    if stock is None:
        return None
    rows = (DailyQuote.objects.filter(stock=stock, is_final=True, trade_date__gt=signal.trade_date)
            .order_by('trade_date').values_list('trade_date', 'open_price', 'close_price')[:max(NODE_HORIZONS)])
    return [(d, float(o) if o is not None else None, float(c) if c is not None else None) for d, o, c in rows]


def _sector_after(signal):
    from .dimensions import load_sector_snapshots

    level, after = 100.0, []
    for trade_date, by_name in load_sector_snapshots(signal.target_kind, lookback_days=160):
        if trade_date <= signal.trade_date:
            continue
        row = by_name.get(signal.target_code)
        pct = row.get('change_pct') if row else None
        if pct is None:
            break  # 缺口之后的水平不可比，节点只算到缺口前
        level *= 1 + pct / 100
        after.append((trade_date, None, level))
        if len(after) > max(NODE_HORIZONS):
            break
    return after


def review_nodes(signal):
    """为一条信号现算 20/60 日节点；数据不可得时返回 None（页面显示「暂无」）。"""
    try:
        if signal.target_kind == 'stock':
            after, base_is_open = _stock_after(signal), True
        elif signal.target_kind == 'index':
            after = [b for b in _index_bars(signal.target_code) if b[0] > signal.trade_date]
            base_is_open = True
        else:
            after, base_is_open = _sector_after(signal), False
    except Exception:  # noqa: BLE001 边界：节点只是展示，取数失败不影响详情页其他部分
        logger.warning("复盘节点取数失败 %s", signal.target_code, exc_info=True)
        return None
    if after is None:
        return None
    bench = _benchmark() if after else {}
    return judge_nodes(after, signal.direction, bench, base_is_open=base_is_open)
