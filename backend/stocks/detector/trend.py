"""趋势状态（det-v2）：收盘价 ZigZag(2×ATR20) 摆动，供信号卡片展示「处于下跌/上涨波段第 X 天」。

第七轮（round7_turn）的结论：探测器信号不出现在转折之前，而是出现在转折之后——
向下信号在 T 日 100% 已处于 ZigZag(2×ATR20) 的向下摆动中（中位距上次向下转折
16 个交易日），向上信号 100% 处于向上摆动（中位 9 日）。所以信号的定位是「趋势
延续」，卡片上标明当前所处波段，比「预判转折」诚实。

定义与 round7 PREREG 的 ZZ2 一致（只用 T 日及以前的数据）：
- ATR20_t = t 及之前 20 日真实波幅的简单均值，TR = max(H, 前收) − min(L, 前收)；
- 上行摆动中记录上次转折以来的最高收盘 E，首个 C_t ≤ E − 2×ATR20_t 的 t 确认向下
  转折，极点 = E 所在日；下行对称；
- 起始状态未定：自起点的最高/最低收盘任一侧先被突破即定方向（不计为转折，
  极点取当时的最高/最低收盘日）。
展示的「第 X 天」= T 距本波段极点（起点）的交易日数，「自 日期」= 极点日。
日线为本站未复权价：除权缺口可能被识别成摆动，属已知局限。
"""

from __future__ import annotations

ATR_WINDOW = 20
ATR_K = 2.0


def _atr(highs, lows, closes, window=ATR_WINDOW):
    """逐日 ATR（简单均值）；不足 window 个有效 TR 的位置为 None。"""
    n = len(closes)
    trs = [None] * n
    for t in range(n):
        h, low, c = highs[t], lows[t], closes[t]
        if h is None or low is None or c is None:
            continue
        pc = closes[t - 1] if t > 0 else None
        if pc is None:
            trs[t] = h - low
        else:
            trs[t] = max(h, pc) - min(low, pc)
    out = [None] * n
    for t in range(window - 1, n):
        win = trs[t - window + 1:t + 1]
        if any(v is None for v in win):
            continue
        out[t] = sum(win) / window
    return out


def zigzag_swings(closes, atr, k=ATR_K):
    """返回 (当前状态 ±1/0, 当前波段极点下标, 当前波段确认下标)，只用传入序列（≤ T）。"""
    n = len(closes)
    st, hi_i, lo_i = 0, 0, 0
    pivot = conf = None
    for t in range(1, n):
        a, c = atr[t], closes[t]
        if a is None or c is None or closes[hi_i] is None or closes[lo_i] is None:
            continue
        if st == 0:
            if c > closes[hi_i]:
                hi_i = t
            if c < closes[lo_i]:
                lo_i = t
            if c <= closes[hi_i] - k * a:
                st, pivot, conf, lo_i = -1, hi_i, t, t
            elif c >= closes[lo_i] + k * a:
                st, pivot, conf, hi_i = 1, lo_i, t, t
            continue
        if st == 1:
            if c > closes[hi_i]:
                hi_i = t
            elif c <= closes[hi_i] - k * a:
                st, pivot, conf, lo_i = -1, hi_i, t, t
        else:
            if c < closes[lo_i]:
                lo_i = t
            elif c >= closes[lo_i] + k * a:
                st, pivot, conf, hi_i = 1, lo_i, t, t
    return st, pivot, conf


def trend_state(ctx):
    """ctx（末根 = T）的趋势状态；无高低价或历史不足以确定方向时返回 None。

    返回 {'direction': 'up'|'down', 'days': X, 'since': 极点日, 'confirmed': 确认日,
          'method': 'ZigZag 2×ATR20'}。
    """
    dates = ctx.get('dates') or []
    closes = ctx.get('close') or []
    highs = ctx.get('high') or []
    lows = ctx.get('low') or []
    n = len(dates)
    if n < ATR_WINDOW + 1 or not (len(closes) == len(highs) == len(lows) == n):
        return None
    atr = _atr(highs, lows, closes)
    st, pivot, conf = zigzag_swings(closes, atr)
    if st == 0 or pivot is None:
        return None
    return {
        'direction': 'up' if st == 1 else 'down',
        'days': n - 1 - pivot,
        'since': str(dates[pivot]),
        'confirmed': str(dates[conf]),
        'method': f'ZigZag {ATR_K:g}×ATR{ATR_WINDOW}',
    }
