"""回测评估指标。

口径（与 03_backtest_feasibility §3.3 一致，并补上重采样检验与 IC）：
- 触发：平静日（calm=1）且该维度投出方向（生产语境）；「无闸门」对照用所有日；
- 段首：同标的、同方向、在交易日历上连续触发的第一天（去掉高度重叠样本）；
- 命中率：review._evaluate_bars 判定（T+1 开盘基期、5 个交易日、±1σ20、不利先到判负）；
- 仓库基线：review._baseline（同标的 T 之前 60 个起点的兑现率）；
- 随机基线：同标的、同方向、评估期内随机日期（平静口径用平静日、无闸门用所有日）。
  解析式：按触发的标的构成加权的池均值，z 视触发相互独立；
  重采样：每个触发从同标的同方向日期池有放回抽一天，重复 B 次得到随机组合的
  命中率分布，单侧 p =(1+#{随机 ≥ 实际})/(B+1)，z =(实际−随机均值)/随机标准差。
  两种方法都**没有校正样本重叠与截面相关**，显著性偏乐观；
- 前瞻超额：T+1 开盘 → T+N 收盘；个股减同窗口沪深300，指数用自身收益（沪深300 对自己的
  超额恒为 0）；按方向取号（down 取反），随机基线同上；
- IC：维度归一化分值 value（符号与方向一致）与 N 日超额的 Spearman 秩相关（混合截面
  与时序的 pooled IC），t ≈ IC·√(n−2)/√(1−IC²)，同样未校正重叠；
- 分组：按年份、按市况（沪深300 截至 T 的 60 日涨跌幅 ±5%）。
"""

from __future__ import annotations

import math
from collections import Counter

import numpy as np
import pandas as pd

NS = (5, 10, 20)
DEFAULT_RESAMPLES = 2000


def to_frame(rows):
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df['date'] = df['date'].astype(str)
    cal = sorted(df['date'].unique())
    idx = {d: i for i, d in enumerate(cal)}
    df['ci'] = df['date'].map(idx)
    for n in NS:
        r, b = f'r{n}', f'b{n}'
        if r not in df:
            df[r] = np.nan
        if b not in df:
            df[b] = np.nan
        rr = pd.to_numeric(df[r], errors='coerce')
        bb = pd.to_numeric(df[b], errors='coerce')
        df[f'x{n}'] = np.where(df['kind'] == 'index', rr, rr - bb)
    for col in ('hit_up', 'hit_down', 'base_up', 'base_down'):
        df[col] = pd.to_numeric(df[col], errors='coerce') if col in df else np.nan
    return df


def _f(x):
    if x is None:
        return None
    x = float(x)
    return None if math.isnan(x) or math.isinf(x) else x


class Pools:
    """同标的、同方向的随机日期池：命中（0/1）与按方向取号的 N 日超额。"""

    def __init__(self, df):
        self.hit, self.fwd = {}, {}
        for code, g in df.groupby('code'):
            for d, sign in (('up', 1), ('down', -1)):
                h = g[f'hit_{d}'].dropna().to_numpy(dtype=float)
                self.hit[(code, d)] = h
                for n in NS:
                    v = (g[f'x{n}'] * sign).dropna().to_numpy(dtype=float)
                    self.fwd[(code, d, n)] = v

    def get(self, code, d, n=None):
        return self.hit.get((code, d)) if n is None else self.fwd.get((code, d, n))


def _resample(keys, observed, pool_get, n_resample, rng):
    """keys: 每个触发的池键；observed: 对应实际值。返回 (obs_mean, rand_mean, p, z, n)。"""
    pairs = [(k, o) for k, o in zip(keys, observed)
             if not (o is None or (isinstance(o, float) and math.isnan(o)))]
    pairs = [(k, o) for k, o in pairs if pool_get(k) is not None and len(pool_get(k))]
    if not pairs:
        return None
    obs = float(np.mean([o for _k, o in pairs]))
    counts = Counter(k for k, _o in pairs)
    total = sum(counts.values())
    analytic_mu = sum(float(np.mean(pool_get(k))) * c for k, c in counts.items()) / total
    analytic_var = sum(float(np.var(pool_get(k))) * c for k, c in counts.items()) / total ** 2
    out = {'n': total, 'obs': obs, 'rand_mean': analytic_mu,
           'z_analytic': (obs - analytic_mu) / math.sqrt(analytic_var) if analytic_var > 0 else None}
    if n_resample:
        sums = np.zeros(n_resample)
        for k, c in counts.items():
            arr = pool_get(k)
            sums += arr[rng.integers(0, len(arr), size=(n_resample, c))].sum(axis=1)
        means = sums / total
        sd = float(means.std(ddof=1)) if n_resample > 1 else 0.0
        out['p_resample'] = float((1 + np.sum(means >= obs - 1e-12)) / (n_resample + 1))
        out['z_resample'] = (obs - float(means.mean())) / sd if sd > 0 else None
    return out


def trigger_metrics(trig, dircol, pools, n_resample=DEFAULT_RESAMPLES, rng=None):
    """一组触发行的命中率/基线/前瞻超额指标。"""
    rng = rng if rng is not None else np.random.default_rng(0)
    res = {'n': int(len(trig)), 'n_up': int((trig[dircol] == 'up').sum()) if len(trig) else 0,
           'n_down': int((trig[dircol] == 'down').sum()) if len(trig) else 0,
           'targets': int(trig['code'].nunique()) if len(trig) else 0}
    if not len(trig):
        return res
    dirs = trig[dircol].to_numpy()
    codes = trig['code'].to_numpy()
    hit = np.where(dirs == 'up', trig['hit_up'], trig['hit_down']).astype(float)
    base = np.where(dirs == 'up', trig['base_up'], trig['base_down']).astype(float)
    ok = ~np.isnan(hit)
    res['judged'] = int(ok.sum())
    if ok.sum():
        res['hit_rate'] = float(hit[ok].mean())
        okb = ok & ~np.isnan(base)
        res['repo_baseline'] = float(base[okb].mean()) if okb.sum() else None
        r = _resample(list(zip(codes, dirs)), list(hit), lambda k: pools.get(*k), n_resample, rng)
        if r:
            res['rand_baseline'] = r['rand_mean']
            res['rand_excess'] = r['obs'] - r['rand_mean']
            res['z'] = _f(r['z_analytic'])
            res['p_resample'] = r.get('p_resample')
            res['z_resample'] = _f(r.get('z_resample'))
    sign = np.where(dirs == 'up', 1.0, -1.0)
    for n in NS:
        v = trig[f'x{n}'].to_numpy(dtype=float) * sign
        m = ~np.isnan(v)
        if m.sum() < 3:
            continue
        vv = v[m]
        entry = {'n': int(m.sum()), 'mean': float(vv.mean()), 'median': float(np.median(vv)),
                 'p25': float(np.percentile(vv, 25)), 'p75': float(np.percentile(vv, 75)),
                 'win': float((vv > 0).mean())}
        r = _resample([(c, d, n) for c, d in zip(codes[m], dirs[m])], list(vv),
                      lambda k: pools.get(*k), n_resample, rng)
        if r:
            entry.update(rand_mean=r['rand_mean'], diff=r['obs'] - r['rand_mean'],
                         z=_f(r['z_analytic']), p_resample=r.get('p_resample'))
        res[f'fwd{n}'] = entry
    return res


def seg_heads(trig, dircol):
    """同标的同方向交易日历连续触发段的首日。"""
    if not len(trig):
        return trig
    t = trig.sort_values(['code', 'ci'])
    cont = ((t['code'].shift(1) == t['code']) & (t['ci'].shift(1) == t['ci'] - 1)
            & (t[dircol].shift(1) == t[dircol]))
    return t[~cont]


def rank_ic(values, rets):
    """Spearman 秩相关；样本 < 30 返回 None。"""
    s = pd.DataFrame({'v': pd.to_numeric(values, errors='coerce'), 'r': rets}).dropna()
    if len(s) < 30 or s['v'].nunique() < 2 or s['r'].nunique() < 2:
        return None
    ic = float(s['v'].rank().corr(s['r'].rank()))
    if math.isnan(ic):
        return None
    t = ic * math.sqrt(len(s) - 2) / math.sqrt(max(1e-12, 1 - ic * ic))
    return {'ic': ic, 'n': int(len(s)), 't': t}


def group_metrics(trig, dircol, pools, by):
    """按 by 列分组的简表（不做重采样，只给解析式基线）。"""
    out = {}
    if not len(trig) or by not in trig:
        return out
    for key, g in trig.groupby(by):
        if key == '' or key is None:
            continue
        m = trigger_metrics(g, dircol, pools, n_resample=0)
        out[str(key)] = {k: m.get(k) for k in ('n', 'n_up', 'n_down', 'hit_rate', 'rand_baseline', 'rand_excess', 'z')}
        out[str(key)]['fwd20_mean'] = (m.get('fwd20') or {}).get('mean')
        out[str(key)]['fwd20_rand'] = (m.get('fwd20') or {}).get('rand_mean')
    return out


def summarize(rows, dims, n_resample=DEFAULT_RESAMPLES, seed=0):
    """rows → 报告 dict（可 JSON 序列化）。"""
    df = to_frame(rows)
    rng = np.random.default_rng(seed)
    if df.empty:
        return {'meta': {'rows': 0}, 'dims': {}, 'signals': {}}
    calm_df = df[df['calm'] == 1]
    pools_calm, pools_all = Pools(calm_df), Pools(df)
    meta = {
        'rows': int(len(df)), 'targets': int(df['code'].nunique()),
        'period': [df['date'].min(), df['date'].max()],
        'calm_rate': {k: float(g['calm'].mean()) for k, g in df.groupby('kind')},
        'calm_rate_all': float(df['calm'].mean()),
        'calm_reasons': (df.loc[df['calm'] == 0, 'calm_reason'].fillna('')
                         .str.replace(r'[\d.]+', '#', regex=True).value_counts().head(8).to_dict()),
        'resamples': n_resample, 'seed': seed,
    }
    kinds = [k for k in ('stock', 'index') if (df['kind'] == k).any()]
    dim_out = {}
    for k in dims:
        if f'{k}_dir' not in df:
            continue
        for kind in kinds + ['all']:
            sub = df if kind == 'all' else df[df['kind'] == kind]
            sub = sub[sub[f'{k}_av'].notna()].copy()
            if not len(sub):
                continue
            sub[f'{k}_dir'] = sub[f'{k}_dir'].fillna('')
            calm_sub = sub[sub['calm'] == 1]
            trig_calm = calm_sub[calm_sub[f'{k}_dir'] != '']
            trig_all = sub[sub[f'{k}_dir'] != '']
            dc = f'{k}_dir'
            entry = {
                'avail_rate_all': float(sub[f'{k}_av'].mean()),
                'avail_rate_calm': float(calm_sub[f'{k}_av'].mean()) if len(calm_sub) else None,
                'vote_rate_calm': float(len(trig_calm) / len(calm_sub)) if len(calm_sub) else None,
                'calm': trigger_metrics(trig_calm, dc, pools_calm, n_resample, rng),
                'calm_heads': trigger_metrics(seg_heads(trig_calm, dc), dc, pools_calm, n_resample, rng),
                'nogate': trigger_metrics(trig_all, dc, pools_all, n_resample, rng),
                'ic_calm': {n: rank_ic(calm_sub[f'{k}_val'], calm_sub[f'x{n}']) for n in NS},
                'ic_all': {n: rank_ic(sub[f'{k}_val'], sub[f'x{n}']) for n in NS},
                'by_year': group_metrics(trig_calm, dc, pools_calm, 'year'),
                'by_regime': group_metrics(trig_calm, dc, pools_calm, 'regime'),
            }
            notes = sub.loc[sub[f'{k}_av'] == 0, f'{k}_note'] if f'{k}_note' in sub else pd.Series(dtype=str)
            entry['top_unavailable'] = (notes.fillna('').str.replace(r'[\d.]+', '#', regex=True)
                                        .value_counts().head(3).to_dict())
            dim_out[f'{k}|{kind}'] = entry
    sig = df[df['sig_dir'].fillna('') != '']
    sig_out = {}
    for kind in kinds + ['all']:
        s = sig if kind == 'all' else sig[sig['kind'] == kind]
        sig_out[kind] = {
            'all_signals': trigger_metrics(s, 'sig_dir', pools_calm, n_resample, rng),
            'active': trigger_metrics(s[s['cycles'] >= 2], 'sig_dir', pools_calm, n_resample, rng),
            'segment_heads': trigger_metrics(s[s['cycles'] == 2], 'sig_dir', pools_calm, n_resample, rng),
            'by_year': group_metrics(s, 'sig_dir', pools_calm, 'year'),
            'by_regime': group_metrics(s, 'sig_dir', pools_calm, 'regime'),
            'severity': s['severity'].fillna('').value_counts().to_dict() if len(s) else {},
        }
    # 多重比较：单维度平静触发的检验次数（维度 × 对象类型，不含 all 汇总）
    m_tests = sum(1 for key, v in dim_out.items() if not key.endswith('|all') and v['calm'].get('p_resample') is not None)
    meta['m_tests'] = m_tests
    meta['bonferroni_alpha'] = 0.05 / m_tests if m_tests else None
    return {'meta': meta, 'dims': dim_out, 'signals': sig_out}
