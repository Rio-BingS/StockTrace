"""截面特征（det-v2）：在扫描当日的股票池内取分位的个股特征与对应维度。

为什么要截面分位而不是固定阈值：第八轮验证里 MB5 的绝对水平在 2019–2026 年间
在 0.18–0.39 之间漂移，固定阈值会随市场整体杠杆情绪整体失效；通过检验的
定义本身就是「同一天、同一股票池内的分位」。所以这里分两步：
1. scan 先对当日全部数据新鲜的个股算出原始特征（只用 T 日及以前的行情、
   T−1 及以前的两融），在有效样本内取分位，写进 ctx['xsec']；
2. 维度评估只读 ctx['xsec']，不自己算分位——单只股票看不到截面。
ctx 没有 'xsec'（离线回测、详情页重算）时维度如实不可用。

分位口径：平均秩 / 有效样本数（并列取平均秩），取值 (0, 1]，越大越拥挤。
有效样本数少于 min_pool 时不给分位（截面太窄，分位没有意义）。
"""

from __future__ import annotations

from .dimensions import _unavailable

# ============================================================
# 原始特征（纯函数，只依赖 ctx）
# ============================================================


def _margin_window(ctx, days):
    """T−days..T−1 的融资明细行；与个股自身的交易日一一对上才返回，否则 None。

    ctx['stock_margin'] 只含信用交易日 < T 的行（build_stock_context 保证）。
    要求最后一行正是个股的上一根日线（T−1）：T−1 尚未披露或停牌错位时不可用，
    不拿更早的数冒充。
    """
    dates = ctx.get('dates') or []
    rows = ctx.get('stock_margin') or []
    if len(dates) < days + 1 or len(rows) < days:
        return None
    want = [str(d) for d in dates[-(days + 1):-1]]
    tail = rows[-days:]
    if [str(r.get('date')) for r in tail] != want:
        return None
    return tail


def margin_buy_ratio(ctx, days=5):
    """MB5 = Σ融资买入额(T−days..T−1) / 融资余额(T−1)。

    返回 (值, 原因)：非两融标的、余额 ≤0、明细不齐时值为 None、原因说明。
    """
    rows = _margin_window(ctx, days)
    if rows is None:
        if not ctx.get('stock_margin'):
            return None, '非两融标的或尚无融资明细'
        return None, f'T−{days}..T−1 融资明细不齐（T−1 未披露或缺日）'
    balance = rows[-1].get('rz')
    if balance is None or balance <= 0:
        return None, '融资余额 ≤ 0'
    buys = [r.get('rz_buy') for r in rows]
    if any(b is None or b < 0 for b in buys):
        return None, '融资买入额缺失'
    return sum(buys) / balance, ''


def days_to_cover(ctx, days=20, min_days=15):
    """DTC = 融资余额(T−1) / 日均成交额(T−days..T−1)；有效成交日少于 min_days 不可用。"""
    rows = _margin_window(ctx, 1)
    if rows is None:
        return None, 'T−1 融资余额不可得'
    balance = rows[-1].get('rz')
    if balance is None or balance <= 0:
        return None, '融资余额 ≤ 0'
    amounts = (ctx.get('amount') or [])[-(days + 1):-1]
    valid = [a for a in amounts if a is not None and a > 0]
    if len(valid) < min_days:
        return None, f'近 {days} 日有效成交额仅 {len(valid)} 天'
    return balance / (sum(valid) / len(valid)), ''


# ============================================================
# 截面分位
# ============================================================


def pct_rank(values):
    """{key: 数值} → {key: 分位}；平均秩 / n，并列取平均秩。"""
    items = sorted(values.items(), key=lambda kv: kv[1])
    n = len(items)
    out = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            out[items[k][0]] = avg_rank / n
        i = j + 1
    return out


def _feature_table(ctxs, fn):
    raw, reasons = {}, {}
    for i, ctx in enumerate(ctxs):
        value, reason = fn(ctx)
        if value is None:
            reasons[i] = reason
        else:
            raw[i] = value
    return raw, reasons


def attach_cross_section(ctxs, params_by_key):
    """对扫描当日的个股 ctx 列表算截面特征与分位，写进各自的 ctx['xsec']。

    params_by_key：{维度 key: params}，只为其中出现的维度计算对应特征
    （margin_crowding → MB5/DTC）。返回 {特征: 有效样本数}，供扫描留痕。
    """
    xsecs = [dict(ctx.get('xsec') or {}) for ctx in ctxs]
    pools = {}
    mc = params_by_key.get('margin_crowding')
    if mc is not None:
        min_pool = int(mc.get('min_pool', 10))
        for name, fn in (
            ('mb5', lambda c: margin_buy_ratio(c, int(mc.get('mb5_days', 5)))),
            ('dtc', lambda c: days_to_cover(c, int(mc.get('dtc_days', 20)), int(mc.get('dtc_min_days', 15)))),
        ):
            raw, reasons = _feature_table(ctxs, fn)
            pct = pct_rank(raw) if len(raw) >= min_pool else {}
            pools[name] = len(raw)
            for i, xs in enumerate(xsecs):
                xs[name] = raw.get(i)
                xs[f'{name}_pct'] = pct.get(i)
                xs[f'{name}_pool'] = len(raw)
                xs[f'{name}_reason'] = reasons.get(i, '')
        for xs in xsecs:
            xs['min_pool'] = min_pool
    for ctx, xs in zip(ctxs, xsecs):
        ctx['xsec'] = xs
    return pools


# ============================================================
# 维度：融资拥挤度（leverage 组，只作否决/升级，不投票）
# ============================================================


def eval_margin_crowding(ctx, params):
    """融资拥挤度：MB5 分位否决向上信号；MB5 或 DTC 分位升级向下信号严重度。

    第八轮通过的定义（round8_veto §5）：
    - 向上信号 ∧ pct_MB5 ≥ veto_pct → 否决（60 日保留集 +3.99pp vs 基准 +0.90，6/6，q=0.021）；
    - 向下信号 ∧ (pct_MB5 ≥ escalate_pct 或 pct_DTC ≥ escalate_pct) → 严重度升一档
      （20 日增强集 +2.20pp vs 未增强 +0.23，6/6，q=0.007），不加票；
    - 非两融标的或余额 ≤0 → 不可用，不否决、不升级。
    """
    xs = ctx.get('xsec')
    if not xs or 'mb5' not in xs:
        return _unavailable('缺少扫描当日股票池截面（仅在日度扫描中计算）')
    if xs.get('mb5') is None:
        return _unavailable(xs.get('mb5_reason') or '融资明细不可用')
    mb5_pct = xs.get('mb5_pct')
    if mb5_pct is None:
        return _unavailable(
            f"股票池内有效样本 {xs.get('mb5_pool', 0)} 只 < {xs.get('min_pool')}，不取分位"
        )
    dtc_pct = xs.get('dtc_pct')
    veto_pct = float(params.get('veto_pct', 0.70))
    esc_pct = float(params.get('escalate_pct', 0.70))
    annotation = {
        'mb5': round(xs['mb5'], 4),
        'mb5_pct': round(mb5_pct, 3),
        'dtc': round(xs['dtc'], 2) if xs.get('dtc') is not None else None,
        'dtc_pct': round(dtc_pct, 3) if dtc_pct is not None else None,
        'pool': xs.get('mb5_pool'),
        'veto_pct': veto_pct,
        'escalate_pct': esc_pct,
    }
    res = {
        'available': True,
        # 只展示：分位 ×100 方便和其他维度并排看，方向留空（不投票）
        'value': round(mb5_pct * 100, 1),
        'direction': None,
        'note': f"MB5 分位 {mb5_pct:.2f}（池内 {xs.get('mb5_pool')} 只）"
                + (f"，DTC 分位 {dtc_pct:.2f}" if dtc_pct is not None else ''),
        'series': [],
        'annotation': annotation,
    }
    if mb5_pct >= veto_pct:
        res['veto'] = {'direction': 'up',
                       'reason': f'融资拥挤，已否决：MB5 分位 {mb5_pct:.2f} ≥ {veto_pct:.2f}'}
    hits = []
    if mb5_pct >= esc_pct:
        hits.append(f'MB5 分位 {mb5_pct:.2f}')
    if dtc_pct is not None and dtc_pct >= esc_pct:
        hits.append(f'DTC 分位 {dtc_pct:.2f}')
    if hits:
        res['escalate'] = {'direction': 'down',
                           'reason': f"融资拥挤，严重度升一档：{'、'.join(hits)} ≥ {esc_pct:.2f}"}
    return res
