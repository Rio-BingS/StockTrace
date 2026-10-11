"""植入形态的合成检测力测试：探测器能不能在随机游走里认出它要找的形态。

做法：生成 n 条随机游走日线（个股口径，含成交额、无股本），每条在后半段植入
一次「平静中的渐进变化」形态；对照组用同一随机种子的未植入序列。两组都走与真实
回测完全相同的 run_target（平静 → 维度 → 投票/周期），比较植入窗口内同向信号率
与对照同窗口的信号率——差值才是检测力，对照率就是该窗口长度下的假阳率。

形态参数是刻画「早期迹象」的假设（波动收窄、温和漂移、量能缓变），不是实证结论；
它衡量的是「探测器对自己声称要找的东西是否敏感」。
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np

from .engine import run_target
from .tradedays import ListCalendar

# 形态：漂移（每日 σ 的倍数，带符号）、窗口内波动缩放、成交量日增长率、窗口长度
PATTERNS = {
    'accumulate_up': {'drift': 0.35, 'vol_scale': 0.6, 'volume_growth': 0.03, 'length': 10},
    'distribute_down': {'drift': -0.35, 'vol_scale': 0.6, 'volume_growth': -0.02, 'length': 10},
}
SIGMA = 1.5          # 日收益标准差（%）
BASE_VOLUME = 1e7


def _business_days(n, start=date(2020, 1, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def make_series(n_days, seed, plant=None, plant_start=None):
    """随机游走 OHLCV；plant 为 PATTERNS 中的参数，从 plant_start 起植入。"""
    rng = np.random.default_rng(seed)
    dates = _business_days(n_days)
    z = rng.standard_normal((n_days, 5))
    items, prev = [], 10.0
    for t in range(n_days):
        s, mu, vf = SIGMA, 0.0, 1.0
        if plant and plant_start is not None and plant_start <= t < plant_start + plant['length']:
            s = SIGMA * plant['vol_scale']
            mu = plant['drift'] * SIGMA
            vf = (1 + plant['volume_growth']) ** (t - plant_start + 1)
        open_ = prev * (1 + 0.3 * s * z[t, 0] / 100)
        close = prev * (1 + (mu + s * z[t, 1]) / 100)
        high = max(open_, close) * (1 + abs(z[t, 2]) * 0.3 * s / 100)
        low = min(open_, close) * (1 - abs(z[t, 3]) * 0.3 * s / 100)
        volume = BASE_VOLUME * math.exp(0.25 * z[t, 4]) * vf
        items.append({'date': dates[t], 'open': open_, 'high': high, 'low': low, 'close': close,
                      'volume': volume, 'amount': volume * close})
        prev = close
    return items


def _hits(rows, direction, lo, hi, dims):
    window = [r for r in rows if lo <= r['date'] <= hi]
    sig = any(r['sig_dir'] == direction for r in window)
    dim = any(r['calm'] and r.get(f'{k}_dir') == direction for r in window for k in dims)
    return sig, dim


def power_test(configs, n_series=20, n_days=250, seed=7, patterns=None):
    """返回 {series, days, patterns: {name: 植入/对照的信号率与维度触发率}}。"""
    patterns = patterns or PATTERNS
    dims = [c['key'] for c in configs if c['enabled'] and c['evaluate'] is not None and 'stock' in c['targets']]
    out = {'series': n_series, 'days': n_days, 'patterns': {}}
    for name, plant in patterns.items():
        direction = 'up' if plant['drift'] > 0 else 'down'
        counts = {'ps': 0, 'pd': 0, 'ns': 0, 'nd': 0}
        for j in range(n_series):
            s = seed * 1000 + j
            start_idx = n_days // 2 + (j * 7) % (n_days // 2 - plant['length'] - 5)
            planted = make_series(n_days, s, plant, start_idx)
            null = make_series(n_days, s)
            cal = ListCalendar([it['date'] for it in planted])
            # 检测窗口：植入第 3 天起到结束后 2 天（形态需要几天才能在维度里显形）
            lo = planted[start_idx + 2]['date']
            hi = planted[min(n_days - 1, start_idx + plant['length'] + 1)]['date']
            for label, items in (('p', planted), ('n', null)):
                rows = run_target({'kind': 'stock', 'code': f'syn{j:03d}', 'name': 'syn'}, items,
                                  calendar=cal, bench_by_date={}, configs=configs, start=items[0]['date'],
                                  end=None, eval_all_days=False, with_outcomes=False)
                sig, dim = _hits(rows, direction, lo, hi, dims)
                counts[label + 's'] += sig
                counts[label + 'd'] += dim
        out['patterns'][name] = {
            'planted_signal_rate': counts['ps'] / n_series, 'null_signal_rate': counts['ns'] / n_series,
            'planted_dim_rate': counts['pd'] / n_series, 'null_dim_rate': counts['nd'] / n_series,
        }
    return out
