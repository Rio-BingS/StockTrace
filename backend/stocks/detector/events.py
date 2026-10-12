"""事件维度（det-v2）：限售解禁压力 lift_pressure。

第九轮（round9_events，T1 判定总表）：解禁前 20 日窗口 [U−20, U−1]，解禁 ≥1%
流通股（L1）的股票相对同日同行业随机股票跑输 2.03pp（主切分 2022–24，t=3.46，
BH q=0.0065，安慰剂 p=0.005），6/6 检验期同向，判为通过；解禁后 [U, U+19]
没有继续跑输（+0.83pp）。所以事件只在「T 之后 20 个交易日内有解禁」时提示，
解禁日当天起失效。它是供给冲击的事件提示：只展示在信号卡片和个股页，不投票。
"""

from __future__ import annotations

import math

from .dimensions import _unavailable


def lift_value(ratio, base=0.01):
    """解禁压力分值：−clip(25 + 25·log2(R/1%), 25, 100)；R=1% → −25，R≥8% → −100。"""
    raw = 25 + 25 * math.log2(ratio / base)
    return -max(25.0, min(100.0, raw))


def eval_lift_pressure(ctx, params):
    """T 之后 horizon 个交易日内的解禁合计比例 ≥ min_ratio 时触发（方向向下，只提示）。

    只看解禁日 > T 的排期（load_upcoming_releases 保证），解禁日当天起失效；
    ctx 的 lift_as_of 必须等于 as_of（周期回看截断后的 ctx 不复用 T 日窗口）。
    """
    releases = ctx.get('restricted_releases')
    if releases is None:
        return _unavailable('解禁日程尚未接入（需运行 backfill_restricted_release）')
    if ctx.get('lift_as_of') != ctx.get('as_of'):
        return _unavailable('解禁窗口只对扫描当日计算')
    horizon = int(params.get('horizon_days', 20))
    min_ratio = float(params.get('min_ratio', 0.01))
    days = (ctx.get('lift_calendar') or [])[:horizon]
    as_of = str(ctx.get('as_of'))
    within = [r for r in releases if as_of < r['date'] and days and r['date'] <= days[-1]]
    total = sum(r.get('ratio') or 0.0 for r in within)
    if not within or total < min_ratio:
        return {'available': True, 'value': 0.0, 'direction': None,
                'note': f'未来 {horizon} 个交易日内无合计 ≥{min_ratio:.0%} 的解禁', 'series': []}
    first = within[0]
    days_to = days.index(first['date']) + 1 if first['date'] in days else None
    value = round(lift_value(total), 1)
    types = []
    for r in within:
        types.extend(t for t in r.get('types', []) if t not in types)
    return {
        'available': True,
        'value': value,
        'direction': 'down',
        'note': (f"未来 {horizon} 个交易日内解禁合计 {total:.2%} 流通股，首个 {first['date']}"
                 + (f'（{days_to} 个交易日后）' if days_to else '')),
        'series': [],
        'annotation': {
            'event': 'lift',
            'ratio_total': round(total, 4),
            'value': value,
            'first_date': first['date'],
            'days_to': days_to,
            'horizon_days': horizon,
            'events': [{'date': r['date'], 'ratio': round(r['ratio'], 4), 'types': r.get('types', [])}
                       for r in within],
            'types': types,
        },
    }
