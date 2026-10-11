"""按时点构造 ctx：T 日的上下文只含 T 及之前的数据（严禁前视）。

与生产口径对齐：
- 价格序列取 T 及之前最多 ctx_bars 根（生产 build_stock_context 的 [:70]、
  fetch_index_daily_series(days=70)），由仓库 _price_ctx 组装；
- 个股附成交额（送转断层识别）与按变动日展开到 ≤T 的流通股本；
- 沪深300 附融资余额：只取数据日 < T 的行（T+1 披露：T 日 17:15 扫描时最新
  只有 T-1 的数），最多 40 行（build_index_context 读最近 40 行快照）。
"""

from __future__ import annotations

from bisect import bisect_left

from ...datasources.float_shares import shares_on_dates
from ..constants import MARGIN_VOTE_CODES
from ..dimensions import _price_ctx

# 与生产 build_index_context / build_stock_context 的 70 根一致；data-align 方向
# 若拉长生产上下文，回测用 --ctx-bars 同步即可
DEFAULT_CTX_BARS = 70
MARGIN_ROWS = 40
# 融资余额最新一行最多落后 T 几个交易日仍算新鲜（T+1 披露的正常状态是 1）
MARGIN_MAX_LAG = 2


def margin_window(margin_rows, margin_dates, as_of, calendar, rows=MARGIN_ROWS):
    """T=as_of 时可见的融资余额序列 [{date, total}]。

    - 只取数据日严格 < T 的行；
    - 源数据（宏观表深市段）有成片缺口：只保留最后一个缺口之后的连续段
      （相当于生产快照断档后重新积累；生产快照本身不校验日期连续性，
      见 03_backtest_feasibility §5.4）；
    - 最新一行早于 T 的前 MARGIN_MAX_LAG 个交易日：源缺口期，如实置空。
    """
    k = bisect_left(margin_dates, as_of)
    win = margin_rows[max(0, k - rows):k]
    cut = 0
    for j in range(1, len(win)):
        a, b = calendar.index_of(win[j - 1][0]), calendar.index_of(win[j][0])
        if a is None or b is None or b - a != 1:
            cut = j
    t_idx = calendar.index_of(as_of)
    if win and t_idx is not None:
        last_idx = calendar.index_of(win[-1][0])
        if last_idx is None or last_idx < t_idx - MARGIN_MAX_LAG:
            return []
    return [{'date': d, 'total': t} for d, t in win[cut:]]


def build_ctx(kind, code, name, items, i, *, ctx_bars=DEFAULT_CTX_BARS, share_hist=None,
              margin_rows=None, margin_dates=None, calendar=None):
    """items[i] 为 T 日；只切 items[:i+1] 的尾部，物理上拿不到 T 之后的数据。"""
    win = items[max(0, i - ctx_bars + 1): i + 1]
    ctx = _price_ctx(kind, code, name, win)
    if kind == 'stock':
        ctx['amount'] = [it.get('amount') for it in win]
        ctx['float_shares'] = shares_on_dates(share_hist, ctx['dates']) if share_hist else None
    elif kind == 'index':
        ctx['margin'] = []
        if code in MARGIN_VOTE_CODES and margin_rows and calendar is not None:
            ctx['margin'] = margin_window(margin_rows, margin_dates, ctx['as_of'], calendar)
    return ctx
