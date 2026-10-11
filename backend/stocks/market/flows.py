"""北向资金 / 涨跌家数情绪 / 大盘主力资金历史与窗口聚合。"""

from __future__ import annotations

import logging

import akshare as ak
import pandas as pd

from ._cache import _cache_get, _cache_meta, _cache_set, _stale_or, _to_float
from ._sources import SourceChain, _safe_df_call
from .periods import period_cache_key

logger = logging.getLogger(__name__)


def fetch_hsgt_flow(ttl=120, force=False):
    if not force:
        cached = _cache_get('hsgt', ttl)
        if cached is not None:
            return cached

    df = _safe_df_call(ak.stock_hsgt_fund_flow_summary_em, source_name='em_hsgt_summary')
    if df is None or df.empty:
        return _stale_or('hsgt', [])

    rows = []
    for _, r in df.iterrows():
        rows.append({
            'trade_date': str(r.get('交易日', '')),
            'type': str(r.get('类型', '')),
            'board': str(r.get('板块', '')),
            'direction': str(r.get('资金方向', '')),
            'trade_status': r.get('交易状态'),
            'net_buy': _to_float(r.get('成交净买额')),
            'net_inflow': _to_float(r.get('资金净流入')),
            'balance': _to_float(r.get('当日资金余额')),
            'up_count': int(r['上涨数']) if pd.notna(r.get('上涨数')) else None,
            'flat_count': int(r['持平数']) if pd.notna(r.get('持平数')) else None,
            'down_count': int(r['下跌数']) if pd.notna(r.get('下跌数')) else None,
            'related_index': str(r.get('相关指数', '')),
            'index_change_pct': _to_float(r.get('指数涨跌幅')),
        })
    # 北向净买额自 2024-08 披露调整后，上游以 0 占位（南向仍为真实值）；
    # 0 占位转 null，避免总览卡显示 "+0.00 亿" 误导。
    # 已知代价：北向某天真为 0（买卖恰好打平）时也会被当作占位符转 null，
    # 当前披露停止的背景下宁可少显示也不显示假 0。
    for row in rows:
        if row.get('direction') == '北向':
            if row.get('net_buy') == 0:
                row['net_buy'] = None
            if row.get('net_inflow') == 0:
                row['net_inflow'] = None
    _cache_set('hsgt', rows)
    return rows


def fetch_market_activity(ttl=120):
    cached = _cache_get('activity', ttl)
    if cached is not None:
        return cached

    df = _safe_df_call(ak.stock_market_activity_legu, source_name='legu_activity')
    if df is None or df.empty:
        return _stale_or('activity', {})

    raw = {}
    for _, r in df.iterrows():
        raw[str(r.get('item', '')).strip()] = r.get('value')

    def pick(*keys):
        for k in keys:
            if k in raw:
                return raw[k]
        return None

    data = {
        'up': _to_float(pick('上涨')),
        'limit_up': _to_float(pick('涨停')),
        'real_limit_up': _to_float(pick('真实涨停')),
        'st_limit_up': _to_float(pick('st st*涨停')),
        'down': _to_float(pick('下跌')),
        'limit_down': _to_float(pick('跌停')),
        'real_limit_down': _to_float(pick('真实跌停')),
        'st_limit_down': _to_float(pick('st st*跌停')),
        'flat': _to_float(pick('平盘')),
        'suspended': _to_float(pick('停牌')),
        'activity': str(pick('活跃度') or ''),
        'stat_time': str(pick('统计日期') or ''),
    }
    _cache_set('activity', data)
    return data


# 东财大盘资金流列：只认「<分项>净流入-净额」精确列名，「-净占比」显式排除（DS-02：
# 两列都含「小单净流入」子串，旧逻辑 rename 成同名列，row.get 拿到 Series 被吞成 None）
_MARKET_FUND_NET_COLS = {
    '主力净流入-净额': 'main_net',
    '超大单净流入-净额': 'super_net',
    '大单净流入-净额': 'large_net',
    '中单净流入-净额': 'mid_net',
    '小单净流入-净额': 'small_net',
}
# 北向兜底源（stock_hsgt_hist_em）：akshare 已 /100 换算成「亿元」，口径为北向成交
# 净买额/资金流入，与「沪深主力净流入（元）」语义不同——换算成元后单列 north_net_buy，
# 绝不写进 main_net（DAT-06）。
_NORTH_HIST_COLS = ('当日成交净买额', '当日资金流入')
_NORTH_HIST_UNIT_FACTOR = 1e8  # 亿元 -> 元
MARKET_FUND_SOURCE_EM = 'eastmoney_market_fund_flow'
MARKET_FUND_SOURCE_NORTH = 'eastmoney_hsgt_hist_north'


def _normalize_market_fund_hist(df, days, source_name):
    """把不同源的资金历史表统一成 items 列表（金额单位统一为元）。

    - 东财主源：main/super/large/mid/small_net 来自精确的「-净额」列，口径沪深两市合计；
    - 北向兜底：只产出 north_net_buy（亿元×1e8=元），main_net 恒为 None，
      返回 available=False（不让前端把北向序列当主力画图）。
    """
    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        return None

    is_north = source_name == MARKET_FUND_SOURCE_NORTH
    work = df.copy()
    colmap = {}
    for c in work.columns:
        s = str(c).strip()
        if '日期' in s or s.lower() == 'date':
            if 'date' not in colmap.values():
                colmap[c] = 'date'
        elif is_north:
            if s in _NORTH_HIST_COLS and 'north_net_buy' not in colmap.values():
                colmap[c] = 'north_net_buy'
        elif s in _MARKET_FUND_NET_COLS:
            colmap[c] = _MARKET_FUND_NET_COLS[s]
        elif s == 'main_net' and 'main_net' not in colmap.values():
            colmap[c] = 'main_net'
    if colmap:
        work = work.rename(columns=colmap)
    if work.columns.duplicated().any():
        # 防御：上游若出现重名列，保留第一列，避免 row.get 返回 Series 被吞成 None
        logger.warning('大盘资金历史存在重复列，保留首列: %s', list(work.columns[work.columns.duplicated()]))
        work = work.loc[:, ~work.columns.duplicated()]

    if 'date' not in work.columns:
        return None

    work['date'] = work['date'].astype(str)
    work = work.sort_values('date').tail(days)

    items = []
    for _, r in work.iterrows():
        item = {'date': str(r.get('date', ''))[:10]}
        if is_north:
            north = _to_float(r.get('north_net_buy'))
            item.update({
                'main_net': None, 'small_net': None, 'mid_net': None,
                'large_net': None, 'super_net': None,
                'north_net_buy': round(north * _NORTH_HIST_UNIT_FACTOR, 2) if north is not None else None,
            })
        else:
            for key in ('main_net', 'small_net', 'mid_net', 'large_net', 'super_net'):
                item[key] = _to_float(r.get(key))
        items.append(item)
    value_key = 'north_net_buy' if is_north else 'main_net'
    if not items or all(i[value_key] is None for i in items):
        # 北向兜底源在披露调整后可能整列为空，全 null 的序列对图表无意义
        return None
    return {
        # 北向兜底不是「大盘主力」：available=False，前端展示 message 而不画主力柱
        'available': not is_north,
        'items': items,
        'message': '',
        'source': source_name,
        'unit': '元',
        'scope': 'north' if is_north else 'sh+sz',
    }


def fetch_market_fund_flow_hist(days=30, ttl=300, force=False):
    """
    大盘资金历史：
    1) 东财 stock_market_fund_flow（主力净流入，最完整）
    2) 东财北向历史 stock_hsgt_hist_em（净买额兜底，口径不同：换算成元后单列
       north_net_buy，main_net 为 None，available=False，不得写入 market_ff 快照）
    带源冷却，避免东财挂了还反复打。
    """
    cache_key = f'market_ff_{days}'
    if not force:
        cached = _cache_get(cache_key, ttl)
        if cached is not None:
            return cached

    def load_em_main():
        return ak.stock_market_fund_flow()

    def load_north_hist():
        return ak.stock_hsgt_hist_em(symbol='北向资金')

    def normalized(name, load):
        # 上游原始表 → 统一结构；解析为空视为该源失败（换下一个源并计入冷却）
        def call():
            raw = load()
            if raw is None or (isinstance(raw, pd.DataFrame) and raw.empty):
                return None
            return _normalize_market_fund_hist(raw, days, name) or None
        return call, name

    # 统一 failover（ARC-03）：未冷却的源优先，全部失败时仍兜底尝试最后一个冷却源
    data, source = SourceChain(last_resort=True).run([
        normalized(MARKET_FUND_SOURCE_EM, load_em_main),
        normalized(MARKET_FUND_SOURCE_NORTH, load_north_hist),
    ])
    if source == MARKET_FUND_SOURCE_NORTH:
        # 北向兜底时提示口径
        data['message'] = (
            '东财大盘主力暂不可用；北向资金净买额口径不同，'
            '已单列为 north_net_buy（元），不作为主力净流入展示'
        )

    if data is None:
        data = _stale_or(
            cache_key,
            {
                'available': False,
                'items': [],
                'message': '大盘资金历史暂不可用（多源均失败）',
                'source': None,
            },
        )
    else:
        _cache_set(cache_key, data)
    return data


def get_market_fund_flow_window(window, ttl=300):
    """大盘主力资金流窗口聚合（东财 fflow 日线，沪 1.000001 + 深 0.399001 同日合计，单位元）。

    口径与 market_ff 快照一致（复用 snapshots.fetch_market_ff_merged_rows）：只合并
    沪深两边同日齐全的行；单边成功时上游视为不可用，不覆盖快照里已有的两市合计。
    快照优先：本地日度快照（收盘后落库）先垫底，上游成功时按日期覆盖/回补。
    上游（push2his）挂掉时窗口仍可用——展示本地已积累的天数并如实提示；
    上游深度约 120 个交易日，超出部分以 coverage_start 如实标注。
    """
    from .etf_flow import _flow_source
    from .snapshots import MARKET_FF_SCOPE, MARKET_FF_UNIT, fetch_market_ff_merged_rows, market_ff_snapshot_rows

    cache_key = period_cache_key('market_ff_win', window)
    cached = _cache_get(cache_key, ttl)
    if cached is not None:
        return cached

    upstream_error = ''
    upstream_rows = []
    try:
        merged, errors = fetch_market_ff_merged_rows()
        upstream_rows = [merged[d] for d in sorted(merged)]
        if not upstream_rows and errors:
            upstream_error = '；'.join(errors)
            logger.warning('大盘资金流窗口上游无数据: %s', upstream_error)
    except Exception as e:  # noqa: BLE001  线路降级
        upstream_error = str(e)
        logger.warning('大盘资金流窗口获取失败: %s', e)

    # 合并：上游为主，快照补缺（上游挂了就全靠快照）
    by_date = {r['date']: r for r in upstream_rows}
    snap_all = market_ff_snapshot_rows()
    snap_in_window = [
        r for r in snap_all
        if r['date'] not in by_date and window.contains(r['date'])
    ]
    rows = sorted(
        list(by_date.values()) + snap_in_window,
        key=lambda r: r['date'],
    )
    rows = [r for r in rows if window.contains(r['date'])]
    # 覆盖起点取上游与快照中最早的一根（可能早于窗口起点，配合 truncated 语义）
    all_dates = [r['date'] for r in upstream_rows] + [r['date'] for r in snap_all]
    coverage_start = min(all_dates) if all_dates else None
    total = lambda key: round(sum(r.get(key) or 0 for r in rows), 2)  # noqa: E731

    if upstream_rows:
        message = ''
    elif snap_in_window:
        message = (
            f'东财上游暂不可用，已展示本地日度快照积累的 {len(snap_in_window)} 个交易日'
            '（每个收盘后自动积累，上游恢复后自动回补历史）'
        )
    elif upstream_error:
        # 上游错误原文已写日志，对外不回显（SEC-07）
        message = '大盘资金流获取失败，稍后可重试'
    else:
        message = '区间内暂无数据'

    data = {
        'available': bool(rows),
        'window': window.meta(),
        'coverage_start': coverage_start,
        'truncated': bool(coverage_start and window.start.isoformat() < coverage_start),
        'items': rows,
        # 口径标注：沪深两市合计，金额单位元（total_* 同单位，前端负责换算展示）
        'scope': MARKET_FF_SCOPE,
        'unit': MARKET_FF_UNIT,
        'summary': {
            'days': len(rows),
            'total_main_net': total('main_net') if rows else None,
            'total_super_net': total('super_net') if rows else None,
            'total_large_net': total('large_net') if rows else None,
            'total_mid_net': total('mid_net') if rows else None,
            'total_small_net': total('small_net') if rows else None,
            'inflow_days': sum(1 for r in rows if (r.get('main_net') or 0) > 0),
            'outflow_days': sum(1 for r in rows if (r.get('main_net') or 0) < 0),
        },
        'message': message,
        'note': (
            f'区间起点早于上游覆盖范围，实际自 {coverage_start} 起计算'
            if coverage_start and window.start.isoformat() < coverage_start else ''
        ),
        'meta': _cache_meta(
            cache_key, ttl,
            _flow_source() + ' + 本站日度快照' if snap_in_window
            else _flow_source() + ' (沪深合计 1.000001+0.399001)',
            bool(rows),
            source_data_date=rows[-1]['date'] if rows else None,
            disclaimer='历史主力净流入来自东方财富，口径为沪深两市合计（单位元）；本地快照由收盘后预热自动积累。',
        ),
    }
    if rows:
        _cache_set(cache_key, data)
    return data


def get_northbound_window(window, ttl=300):
    """北向资金净买额窗口聚合（基于历史序列切片）。"""
    from .institutions import NORTH_DISCLOSURE_MSG, fetch_northbound_flow_series

    cache_key = period_cache_key('north_win', window)
    cached = _cache_get(cache_key, ttl)
    if cached is not None:
        return cached

    series = fetch_northbound_flow_series(days=400)
    items = [i for i in series.get('items', []) if window.contains(i.get('date'))]
    valid = [i for i in items if i.get('net_buy') is not None]
    total_net = round(sum(i['net_buy'] for i in valid), 2)
    # 区间内有数据但净买额全 null 时如实标不可用
    has_net = bool(valid)

    data = {
        'available': has_net,
        'window': window.meta(),
        'coverage_start': items[0]['date'] if items else None,
        'items': items,
        'summary': {
            'days': len(valid),
            'total_net_buy': total_net if has_net else None,
            'inflow_days': sum(1 for i in valid if i['net_buy'] > 0),
            'outflow_days': sum(1 for i in valid if i['net_buy'] < 0),
        },
        'message': '' if has_net else (NORTH_DISCLOSURE_MSG if items else '区间内暂无北向数据'),
        'meta': _cache_meta(
            cache_key, ttl, series.get('source') or 'stock_hsgt_hist_em', has_net,
            source_data_date=items[-1]['date'] if items else None,
            disclaimer='北向资金为历史成交净买额口径，存在披露口径变化。',
        ),
    }
    if items:
        _cache_set(cache_key, data)
    return data
