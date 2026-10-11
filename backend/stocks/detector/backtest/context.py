"""按时点构造 ctx：T 日的上下文只含 T 及之前的数据（严禁前视）。

与生产口径对齐（data-align 之后）：
- 价格序列取 T 及之前最多 ctx_bars 根（默认 CONTEXT_BARS，与生产
  build_stock_context / build_index_context 一致），由仓库 _price_ctx 组装；
- 个股附成交额（送转断层识别）、按变动日展开到 ≤T 的流通股本，以及送转/新股
  校正后的量能 volume_adj（仓库 adjusted_volume_series，#15）；指数 volume_adj
  即原始成交量；
- 沪深300 附融资余额：与 load_margin_series 同一口径——按交易日轴对齐，末点 =
  T 的前一交易日（T+1 披露），轴长 MARGIN_CONTEXT_DAYS，缺失日为 None（#8/#9）。
  回测缓存（margin.csv）只有融资融券余额 total，没有融资余额 rz：这里用 total
  代理 rz（融资余额约占 98%~99%，相对漂移口径差别很小），报告里写明。
"""

from __future__ import annotations

from bisect import bisect_left

from ...datasources.float_shares import shares_on_dates
from ..constants import CONTEXT_BARS, MARGIN_CONTEXT_DAYS, MARGIN_VOTE_CODES
from ..dimensions import _listing_date, _price_ctx, adjusted_volume_series

# 与生产 build_index_context / build_stock_context 的 CONTEXT_BARS 一致
DEFAULT_CTX_BARS = CONTEXT_BARS
# 融资余额缓存只有 total：用它代理 rz（见模块 docstring）
MARGIN_PROXY_NOTE = '回测两融缓存只有融资融券余额，margin_north 以其代理融资余额 rz'


def margin_series(margin_by_date, first_date, as_of, calendar, n=MARGIN_CONTEXT_DAYS):
    """T=as_of 时可见的融资余额序列 [{date, rz, total}]（与生产 load_margin_series 同口径）。

    - 轴：日历上 T 之前（不含）的 n 个交易日，末点 = T−1；轴起点不早于缓存里最早
      一行（「按现有可得长度」，不在头部堆 None）；
    - 轴上缓存没有数的交易日（源数据缺口）为 None，维度取窗时如实不可用；
    - T−1 没有数时末点为 None：维度如实不可用，不拿更早的数冒充最新。
    """
    ci = calendar.index_of(as_of)
    if ci is None or ci < 1 or not margin_by_date:
        return []
    axis = calendar.isos[max(0, ci - n):ci]
    axis = [d for d in axis if d >= first_date]
    out = []
    for d in axis:
        total = margin_by_date.get(d)
        out.append({'date': d, 'rz': total, 'total': total})
    return out


def margin_window(margin_rows, margin_dates, as_of, calendar, n=MARGIN_CONTEXT_DAYS):
    """兼容旧调用：margin_rows=[(iso, total)] 升序 → margin_series。"""
    if not margin_rows:
        return []
    k = bisect_left(margin_dates, as_of)
    visible = dict(margin_rows[:k])
    return margin_series(visible, margin_rows[0][0], as_of, calendar, n=n)


def build_ctx(kind, code, name, items, i, *, ctx_bars=DEFAULT_CTX_BARS, share_hist=None,
              margin_rows=None, margin_dates=None, calendar=None):
    """items[i] 为 T 日；只切 items[:i+1] 的尾部，物理上拿不到 T 之后的数据。"""
    win = items[max(0, i - ctx_bars + 1): i + 1]
    ctx = _price_ctx(kind, code, name, win)
    if kind == 'stock':
        ctx['amount'] = [it.get('amount') for it in win]
        ctx['float_shares'] = shares_on_dates(share_hist, ctx['dates']) if share_hist else None
        ctx['volume_adj'], ctx['volume_basis'] = adjusted_volume_series(
            ctx, listing_date=_listing_date(share_hist),
        )
    elif kind == 'index':
        ctx['volume_adj'] = list(ctx['volume'])
        ctx['volume_basis'] = 'volume'
        ctx['margin'] = []
        ctx['margin_as_of'] = None
        if code in MARGIN_VOTE_CODES and margin_rows and calendar is not None:
            ctx['margin'] = margin_window(margin_rows, margin_dates, ctx['as_of'], calendar)
            last = [m['date'] for m in ctx['margin'] if m['rz'] is not None]
            ctx['margin_as_of'] = last[-1] if last else None
    return ctx
