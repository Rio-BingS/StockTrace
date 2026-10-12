"""截面特征（det-v2）：在扫描日的参照截面里取分位的个股特征与对应维度。

为什么要截面分位而不是固定阈值：第八轮验证里 MB5 的绝对水平在 2019–2026 年间
在 0.18–0.39 之间漂移，固定阈值会随市场整体杠杆情绪整体失效；通过检验的
定义本身就是「同一天、同一截面内的分位」，回测截面是沪深300+中证500 成分。

参照截面（与回测尽量对齐，不在约 20 只自选股里取分位）：
- MB5：StockMarginDaily 在 T−5..T−1 的全市场两融标的明细（交易所全量落库），
  只取沪深 A 股代码（60/68/00/30 开头，剔除 ETF 等）。生产库没有沪深300/中证500
  的历史成分表（只有离线回测缓存里的沪深300），所以截面是「全部 A 股两融标的」，
  比回测宽——这是有意记录的偏离，见 PR 说明。
- DTC：需要逐股 20 日成交额，全市场成交额没有可靠的本地来源，只能用本地有日线的
  个股（即当日扫描的自选股）；有效样本达不到 min_pool 时 DTC 不参与，向下升级
  只看 MB5——相对回测登记定义的偏离。
- ID20：同理只有本地日线，样本达不到 min_pool 时不给分位、不打标签。
min_pool 按全市场截面设定（默认 200），本地二三十只的池子不再取分位。

两步：
1. scan 对当日全部数据新鲜的个股算出原始特征（只用 T 日及以前的行情、
   T−1 及以前的两融），并从库里读全市场 MB5 截面，写进 ctx['xsec']；
2. 维度评估只读 ctx['xsec']，不自己算分位——单只股票看不到截面。
ctx 没有 'xsec'（离线回测、详情页重算）时维度如实不可用。

分位口径：平均秩 / 有效样本数（并列取平均秩），取值 (0, 1]，越大越拥挤。
有效样本数少于 min_pool 时不给分位（截面太窄，分位没有意义）。
"""

from __future__ import annotations

import math

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


def intraday_sum(ctx, window=20, min_valid=15):
    """ID20 = Σ ln(收盘/开盘)，t ∈ [T−window+1, T]；有效日少于 min_valid 不可用。

    日内收益与复权无关（同一天开收盘同比例调整），直接用本地日线。
    """
    opens = (ctx.get('open') or [])[-window:]
    closes = (ctx.get('close') or [])[-window:]
    terms = [math.log(c / o) for o, c in zip(opens, closes)
             if o is not None and c is not None and o > 0 and c > 0]
    if len(terms) < min_valid:
        return None, f'近 {window} 日有效日线仅 {len(terms)} 根'
    return sum(terms), ''


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


# 全市场截面只取沪深 A 股：两融明细里还有 ETF/LOF（51/56/58/15/16 开头），
# 回测截面是股票，混进基金会改变分位
_A_SHARE_PREFIXES = ('60', '68', '00', '30')
DEFAULT_MIN_POOL = 200


def load_mb5_universe(trade_date, days=5):
    """T 日扫描用的全市场 MB5 截面：{'dates': [T−days..T−1 ISO], 'values': {code: MB5}}。

    窗口 = StockMarginDaily 里早于 T、沪深两所都已落库的最近 days 个信用交易日；
    某所某日没落齐的日子不算（半个截面不能当全市场）。窗口内每天都有明细、
    T−1 余额 > 0、买入额齐全的 A 股两融标的才进截面。库里不足 days 天返回 None。
    """
    from ..models import StockMarginDaily

    if isinstance(trade_date, str):
        from datetime import date as _date
        trade_date = _date.fromisoformat(trade_date)
    seen = {}
    pairs = (
        StockMarginDaily.objects.filter(trade_date__lt=trade_date)
        .values_list('trade_date', 'exchange').distinct().order_by('-trade_date')[:days * 6]
    )
    for day, exchange in pairs:
        seen.setdefault(day, set()).add(exchange)
    complete = sorted(d for d, ex in seen.items() if ex >= {'sse', 'szse'})[-days:]
    if len(complete) < days:
        return None
    rows = {}
    for code, day, rz, rz_buy in (
        StockMarginDaily.objects.filter(trade_date__in=complete)
        .values_list('code', 'trade_date', 'rz', 'rz_buy')
    ):
        if code.startswith(_A_SHARE_PREFIXES):
            rows.setdefault(code, {})[day] = (rz, rz_buy)
    last = complete[-1]
    values = {}
    for code, by_day in rows.items():
        if len(by_day) != days:
            continue
        balance = by_day[last][0]
        buys = [by_day[d][1] for d in complete]
        if balance is None or balance <= 0 or any(b is None or b < 0 for b in buys):
            continue
        values[code] = sum(buys) / balance
    return {'dates': [d.isoformat() for d in complete], 'values': values}


def _attach_mb5(ctxs, xsecs, mc, universe, min_pool):
    """MB5：个股自身值来自 ctx，分位在全市场两融截面里取。"""
    days = int(mc.get('mb5_days', 5))
    raw, reasons = _feature_table(ctxs, lambda c: margin_buy_ratio(c, days))
    values = (universe or {}).get('values') or {}
    window = (universe or {}).get('dates') or []
    pool = len(values)
    pct = pct_rank(values) if pool >= min_pool else {}
    for i, (ctx, xs) in enumerate(zip(ctxs, xsecs)):
        xs['mb5'] = raw.get(i)
        xs['mb5_pool'] = pool
        xs['mb5_pct'] = None
        xs['mb5_reason'] = reasons.get(i, '')
        if i not in raw:
            continue
        want = [str(d) for d in (ctx.get('dates') or [])[-(days + 1):-1]]
        if universe is None:
            xs['mb5_reason'] = '全市场两融明细未落库，无法取截面分位'
        elif want != window:
            xs['mb5_reason'] = (f"全市场两融截面窗口 {window[0] if window else '-'}~"
                                f"{window[-1] if window else '-'} 与个股 T−{days}..T−1 不一致"
                                '（T−1 明细尚未落齐或停牌错位）')
        elif not pct:
            xs['mb5_reason'] = f'全市场两融截面有效样本 {pool} 只 < {min_pool}，不取分位'
        elif ctx.get('code') not in pct:
            xs['mb5_reason'] = '不在全市场 A 股两融截面内（非 A 股或明细不齐）'
        else:
            xs['mb5_pct'] = pct[ctx['code']]
    return pool


def attach_cross_section(ctxs, params_by_key, mb5_universe=None):
    """对扫描当日的个股 ctx 列表算截面特征与分位，写进各自的 ctx['xsec']。

    params_by_key：{维度 key: params}，只为其中出现的维度计算对应特征
    （margin_crowding → MB5/DTC，intraday_reversal → ID20）。
    mb5_universe：load_mb5_universe 的结果（全市场 MB5 截面）；None 时 MB5 不给分位。
    DTC/ID20 只有本地日线，在 ctxs 内取分位，样本不足 min_pool 时不给分位。
    返回 {特征: 有效样本数}，供扫描留痕。
    """
    xsecs = [dict(ctx.get('xsec') or {}) for ctx in ctxs]
    pools = {}
    mc = params_by_key.get('margin_crowding')
    if mc is not None:
        min_pool = int(mc.get('min_pool', DEFAULT_MIN_POOL))
        pools['mb5'] = _attach_mb5(ctxs, xsecs, mc, mb5_universe, min_pool)
        raw, reasons = _feature_table(
            ctxs, lambda c: days_to_cover(c, int(mc.get('dtc_days', 20)), int(mc.get('dtc_min_days', 15))))
        pct = pct_rank(raw) if len(raw) >= min_pool else {}
        pools['dtc'] = len(raw)
        for i, xs in enumerate(xsecs):
            xs['dtc'] = raw.get(i)
            xs['dtc_pct'] = pct.get(i)
            xs['dtc_pool'] = len(raw)
            xs['dtc_reason'] = reasons.get(i, '') or (
                '' if i in pct else f'本地日线有效样本 {len(raw)} 只 < {min_pool}，DTC 不参与')
            xs['min_pool'] = min_pool
    ir = params_by_key.get('intraday_reversal')
    if ir is not None:
        min_pool = int(ir.get('min_pool', DEFAULT_MIN_POOL))
        raw, reasons = _feature_table(
            ctxs, lambda c: intraday_sum(c, int(ir.get('window', 20)), int(ir.get('min_valid', 15))))
        pct = pct_rank(raw) if len(raw) >= min_pool else {}
        pools['id20'] = len(raw)
        for i, xs in enumerate(xsecs):
            xs['id20'] = raw.get(i)
            xs['id20_pct'] = pct.get(i)
            xs['id20_pool'] = len(raw)
            xs['id20_reason'] = reasons.get(i, '')
            xs['id20_min_pool'] = min_pool
    for ctx, xs in zip(ctxs, xsecs):
        ctx['xsec'] = xs
    return pools


# ============================================================
# 维度：融资拥挤度（leverage 组，只作否决/升级，不投票）
# ============================================================


def eval_margin_crowding(ctx, params):
    """融资拥挤度：MB5 分位否决向上信号；MB5 或 DTC 分位升级向下信号严重度。

    第八轮通过的定义（round8_veto §5，截面为沪深300+中证500；这里 MB5 用全市场
    A 股两融标的、DTC 只在本地样本 ≥ min_pool 时参与，见模块说明）：
    - 向上信号 ∧ pct_MB5 ≥ veto_pct → 否决（60 日保留集 +3.99pp vs 基准 +0.90，6/6，q=0.021）；
    - 向下信号 ∧ (pct_MB5 ≥ escalate_pct 或 pct_DTC ≥ escalate_pct) → 严重度升一档
      （20 日增强集 +2.20pp vs 未增强 +0.23，6/6，q=0.007），不加票；
    - 非两融标的或余额 ≤0 → 不可用，不否决、不升级。
    """
    xs = ctx.get('xsec')
    if not xs or 'mb5' not in xs:
        return _unavailable('缺少扫描当日截面（仅在日度扫描中计算）')
    if xs.get('mb5') is None:
        return _unavailable(xs.get('mb5_reason') or '融资明细不可用')
    mb5_pct = xs.get('mb5_pct')
    if mb5_pct is None:
        return _unavailable(xs.get('mb5_reason') or '全市场两融截面不可用，不取分位')
    dtc_pct = xs.get('dtc_pct')
    veto_pct = float(params.get('veto_pct', 0.70))
    esc_pct = float(params.get('escalate_pct', 0.70))
    annotation = {
        'mb5': round(xs['mb5'], 4),
        'mb5_pct': round(mb5_pct, 3),
        'dtc': round(xs['dtc'], 2) if xs.get('dtc') is not None else None,
        'dtc_pct': round(dtc_pct, 3) if dtc_pct is not None else None,
        'pool': xs.get('mb5_pool'),
        'dtc_pool': xs.get('dtc_pool'),
        'veto_pct': veto_pct,
        'escalate_pct': esc_pct,
    }
    res = {
        'available': True,
        # 只展示：分位 ×100 方便和其他维度并排看，方向留空（不投票）
        'value': round(mb5_pct * 100, 1),
        'direction': None,
        'note': f"MB5 分位 {mb5_pct:.2f}（全市场两融 {xs.get('mb5_pool')} 只）"
                + (f"，DTC 分位 {dtc_pct:.2f}" if dtc_pct is not None
                   else f"，{xs.get('dtc_reason') or 'DTC 不可用'}，向下升级只看 MB5"),
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


# ============================================================
# 维度：日内追买过热（price 组，只展示，不投票）
# ============================================================


def eval_intraday_reversal(ctx, params):
    """ID20 截面分位 ≥ tag_pct 时标「日内追买过热」，只展示。

    第八轮 §4A：ID20 是稳定的截面反转因子（rank IC −0.043 / −0.049，20/60 日，
    6/6 段，BH q=0.009；Q5 作向下 60 日 +1.30pp，q=0.014），但对现有向下信号
    没有增量（§4B 全部未通过），所以不投票、不改信号，只在卡片上标出来。
    """
    xs = ctx.get('xsec')
    if not xs or 'id20' not in xs:
        return _unavailable('缺少扫描当日截面（仅在日度扫描中计算）')
    if xs.get('id20') is None:
        return _unavailable(xs.get('id20_reason') or '日线不足')
    pct = xs.get('id20_pct')
    if pct is None:
        return _unavailable(
            f"本地日线有效样本 {xs.get('id20_pool', 0)} 只 < {xs.get('id20_min_pool')}，不取分位")
    tag_pct = float(params.get('tag_pct', 0.80))
    hot = pct >= tag_pct
    res = {
        'available': True,
        'value': round(pct * 100, 1),
        'direction': None,
        'note': f"ID20 {xs['id20'] * 100:+.1f}%，分位 {pct:.2f}（本地日线 {xs.get('id20_pool')} 只）"
                + ('，日内追买过热' if hot else ''),
        'series': [],
    }
    if hot:
        res['annotation'] = {
            'tag': '日内追买过热',
            'id20': round(xs['id20'], 4),
            'id20_pct': round(pct, 3),
            'tag_pct': tag_pct,
            'pool': xs.get('id20_pool'),
        }
    return res
