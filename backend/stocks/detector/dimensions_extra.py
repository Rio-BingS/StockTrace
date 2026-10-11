"""探测器候选维度（试验区）：量价相关性维度。

为什么单独成文件：这里的维度都是「回测证明有效再上线」的候选
（见 detector-research/00_plan.md 阶段 4），注册表里默认 enabled=False；
与 dimensions.py 的线上维度隔开，阈值方向的改动不会互相踩踏。数学辅助
函数直接复用 dimensions.py（_gated / _std / _chain_return 等），口径一致。

约定同 dimensions.py：
- eval_* 签名 evaluate(ctx, params)，返回 {available, value, direction, note, series}；
- 数据不足一律 available=False，绝不补 0。
"""

from __future__ import annotations

import math

from .dimensions import (
    _chain_return,
    _gated,
    _result,
    _return_series,
    _series_points,
    _std_strict,
    _unavailable,
)

# ============================================================
# 量价相关性 / 量能领先（vp_corr）
# ============================================================

# Spearman 相关系数的 Fisher z 方差修正（Fieller 等 1957）：var(atanh ρs) ≈ 1.06/(n-3)
_SPEARMAN_VAR = 1.06
# atanh 在 ±1 处发散：窗口内完全单调时截断，避免 z 无穷大
_RHO_CLIP = 0.999


def _ranks(vals):
    """平均秩（并列取平均），供 Spearman 相关使用。"""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman(xs, ys):
    """Spearman 秩相关；任一侧无变化（常数）返回 None。

    为什么用秩相关而不是 Pearson：单日天量（解禁、指数调样、送转首日）会让
    Pearson 被一个点主导，秩相关对离群点稳健。
    """
    rx, ry = _ranks(xs), _ranks(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def _corr_z(rho, n):
    """相关系数 → 近似标准正态的 z 值（零假设「不相关」下 ~N(0,1)）。

    这就是「按自身分布单位出阈值」：不同窗口长度、不同品种的相关系数
    被换算到同一把 z 尺上，min/mild/max 直接以 z 为单位。
    """
    if rho is None or n <= 3:
        return None
    rho = max(-_RHO_CLIP, min(_RHO_CLIP, rho))
    return math.atanh(rho) * math.sqrt((n - 3) / _SPEARMAN_VAR)


def _abnormal_log_volume(vols, base):
    """异常对数量能：log(V_t) − 前 base 日 log(V) 均值；窗口不齐或量 ≤0 为 None。

    取对数差而不是量比：成交量右偏，对数后近似对称；减去滚动基期去掉量能
    水平与送转台阶（台阶只在 base 天内影响一个点，秩相关下影响有限）。
    """
    logs = [math.log(v) if v is not None and v > 0 else None for v in vols]
    out = [None] * len(vols)
    for i in range(base, len(vols)):
        win = logs[i - base:i]
        if logs[i] is None or any(x is None for x in win):
            continue
        out[i] = logs[i] - sum(win) / base
    return out


def _lag_corr_z(xs, ys, end, window, lag):
    """以 end 结尾的 window 对 (xs[j-lag], ys[j]) 的 Spearman z；缺值返回 None。"""
    start = end - window + 1
    if start - lag < 0:
        return None
    px = xs[start - lag:end - lag + 1]
    py = ys[start:end + 1]
    if any(v is None for v in px) or any(v is None for v in py):
        return None
    return _corr_z(_spearman(px, py), window)


def eval_vp_corr(ctx, params):
    """量价相关性 / 量能领先（指数/个股）：昨日异常量能与今日收益的秩相关 z 值。

    依据（02_external_research §1.2/§2）：中金「量能领先」因子（换手与之后收益
    的相关）IC_IR +0.52，「量价同步」因子 IC_IR −1.07——只有量先于价是正向证据，
    量价同步（放量当天就涨）是信息扩散后期/羊群，反而偏负。

    口径：
    - lead = corr_s(异常量能[t−1], 收益[t])，近 window 对；z 化为标准正态单位；
      z>0 = 放量之后次日倾向上涨（资金先进、价格随后跟上）→ up，反之 down；
    - sync = corr_s(异常量能[t], 收益[t])：同步强于领先（|z_sync| ≥ |z_lead| +
      sync_margin）说明是「放量当天就动」，不是量先于价，如实不投票；
    - 价格尚未大动：近 price_days 日链式涨跌幅 / (σ×√price_days) 的绝对值须
      ≤ price_z_max（σ 取此前 sigma_window 日的日涨跌幅标准差，不含当前窗口），
      价格已经走出来就是追认而非早期；
    - 温和带与噪声闸门复用 _gated：min/mild/max 以 z 为单位，|z| 还须超出
      自身近 noise_window 个历史 z 的 noise_pct 分位。
    只用日线 OHLCV：指数/个股的 volume；无 volume 但有 amount（成交额）序列时
    用成交额（为板块留口子，板块快照目前没有成交额，注册表 targets 不含板块）。
    """
    vols = ctx.get('volume')
    if not vols or all(v is None for v in vols):
        vols = ctx.get('amount')
    rets = _return_series(ctx)
    if not vols or rets is None or len(vols) != len(rets):
        return _unavailable('缺少成交量/价格序列')
    window, base = int(params['window']), int(params['vol_base'])
    if len(vols) < base + window + 2:
        return _unavailable('量价历史不足（相关窗口不齐）')

    av = _abnormal_log_volume(vols, base)
    n = len(vols)
    leads = [_lag_corr_z(av, rets, i, window, 1) for i in range(n)]
    raw = leads[-1]
    if raw is None:
        return _unavailable('量价相关窗口存在缺失数据')
    sync = _lag_corr_z(av, rets, n - 1, window, 0)
    series = _series_points(ctx['dates'], leads)

    value, direction, note = _gated(raw, leads[:-1], params)
    if direction is not None and sync is not None \
            and abs(sync) >= abs(raw) + float(params.get('sync_margin', 0.0)):
        direction, note = None, f'量价同步（z={sync:.2f}）不弱于量能领先，非量先于价'
    if direction is not None:
        k = int(params['price_days'])
        sw = int(params['sigma_window'])
        drift = _chain_return(rets, k)
        hist = rets[max(0, n - k - sw):n - k]
        # 收盘价现算的首个涨跌幅恒为 None（无前收），只去掉开头的 None；中间缺口仍判不齐
        while hist and hist[0] is None:
            hist = hist[1:]
        sigma = _std_strict(hist) if len(hist) >= max(10, sw // 2) else None
        if drift is None or not sigma:
            direction, note = None, '价格基准窗口不齐，无法判断价格是否已动'
        else:
            pz = drift / (sigma * math.sqrt(k))
            if abs(pz) > float(params['price_z_max']):
                direction, note = None, f'近{k}日价格已明显移动（{pz:+.1f}σ），非量先于价'
    return _result(value, direction, note, series)
