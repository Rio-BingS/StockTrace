"""指数行情：主要指数现价与多指数归一化走势。"""

from __future__ import annotations

import akshare as ak
import pandas as pd

from ._cache import cache_get, _cache_meta, cache_set, stale_or, _to_float
from ._sources import _safe_df_call

MAJOR_INDICES = [
    ('sh000001', '上证指数'),
    ('sz399001', '深证成指'),
    ('sz399006', '创业板指'),
    ('sh000300', '沪深300'),
    ('sh000016', '上证50'),
    ('sh000688', '科创50'),
    ('sh000852', '中证1000'),
    ('sz399005', '中小100'),
]

# 走势图对比用（新浪日线代码）
TREND_INDICES = [
    ('sh000001', '上证指数'),
    ('sz399001', '深证成指'),
    ('sz399006', '创业板指'),
    ('sh000300', '沪深300'),
    ('sh000688', '科创50'),
]


def fetch_major_indices(ttl=60):
    cached = cache_get('indices', ttl)
    if cached is not None:
        return cached

    df = _safe_df_call(ak.stock_zh_index_spot_sina, source_name='sina_index_spot')
    if df is None or df.empty:
        return stale_or('indices', [])

    by_code = {str(r['代码']): r for _, r in df.iterrows()}
    result = []
    for code, fallback_name in MAJOR_INDICES:
        row = by_code.get(code)
        if row is None:
            result.append({
                'code': code,
                'name': fallback_name,
                'price': None,
                'change': None,
                'change_pct': None,
                'open': None,
                'high': None,
                'low': None,
                'prev_close': None,
                'volume': None,
                'turnover': None,
            })
            continue
        result.append({
            'code': code,
            'name': str(row.get('名称') or fallback_name),
            'price': _to_float(row.get('最新价')),
            'change': _to_float(row.get('涨跌额')),
            'change_pct': _to_float(row.get('涨跌幅')),
            'open': _to_float(row.get('今开')),
            'high': _to_float(row.get('最高')),
            'low': _to_float(row.get('最低')),
            'prev_close': _to_float(row.get('昨收')),
            'volume': _to_float(row.get('成交量')),
            'turnover': _to_float(row.get('成交额')),
        })
    cache_set('indices', result)
    return result


def fetch_index_trend(days=120, ttl=300, start=None, end=None):
    """多指数收盘价序列 + 归一化涨跌幅（相对窗口首日）。

    days=取最近 N 个交易日；传 start/end（YYYY-MM-DD）则按日期切片；
    只传 end 时取截至 end 的最近 N 个交易日。
    """
    cache_key = f'trend_{days}_{start or ""}_{end or ""}'
    cached = cache_get(cache_key, ttl)
    if cached is not None:
        return cached

    series = []
    for code, name in TREND_INDICES:
        df = _safe_df_call(
            ak.stock_zh_index_daily, symbol=code, source_name=f'sina_index_daily_{code}'
        )
        if df is None or df.empty:
            continue
        df = df.copy()
        df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
        df = df.sort_values('date')
        # start/end 各自独立生效（DAT-20）：只传 end 时取 end 及之前最近 days 根
        if start:
            df = df[df['date'] >= start]
        if end:
            df = df[df['date'] <= end]
        if not start:
            df = df.tail(days)
        if df.empty:
            continue
        closes = []
        for _, r in df.iterrows():
            closes.append({
                'date': str(r['date']),
                'close': _to_float(r.get('close')),
                'volume': _to_float(r.get('volume')),
            })
        if not closes:
            continue
        base = closes[0]['close']
        for item in closes:
            if base and item['close'] is not None:
                item['norm_pct'] = round((item['close'] / base - 1) * 100, 3)
            else:
                item['norm_pct'] = None
        series.append({
            'code': code,
            'name': name,
            'items': closes,
            'period_change_pct': closes[-1].get('norm_pct'),
        })

    data = {
        'available': bool(series),
        'days': None if start else days,
        'start': start,
        'end': end,
        'series': series,
        'message': '' if series else '指数日线暂不可用',
    }
    cache_set(cache_key, data)
    return data


def fetch_index_daily_series(code, days=70, ttl=300):
    """单指数日线完整 OHLCV 序列（探测器平静判定与波动维度供数）。

    为什么不复用 fetch_index_trend：走势接口为多指数归一化对比只保留了
    close/volume，探测器做「振幅低位」判定需要 high/low。上游接口完全
    相同（新浪指数日线 stock_zh_index_daily），本函数只是按单指数保留
    完整 OHLCV 并走同一套 TTL 缓存——不是新数据源，是同一接口的薄扩展。
    上游失败如实 available=False，由探测器把该指数当天跳过。
    """
    cache_key = f'idx_series_{code}_{days}'
    cached = cache_get(cache_key, ttl)
    if cached is not None:
        return cached

    df = _safe_df_call(ak.stock_zh_index_daily, symbol=code, source_name=f'sina_index_daily_{code}')
    if df is None or df.empty:
        return {'available': False, 'items': [], 'message': '指数日线暂不可用'}

    work = df.copy()
    work['date'] = pd.to_datetime(work['date']).dt.strftime('%Y-%m-%d')
    work = work.sort_values('date').tail(days)
    items = []
    for _, r in work.iterrows():
        items.append({
            'date': str(r['date']),
            'open': _to_float(r.get('open')),
            'high': _to_float(r.get('high')),
            'low': _to_float(r.get('low')),
            'close': _to_float(r.get('close')),
            'volume': _to_float(r.get('volume')),
        })
    data = {
        'available': bool(items),
        'items': items,
        'message': '' if items else '指数日线暂不可用',
    }
    if items:
        cache_set(cache_key, data)
    return data

# 乐咕乐股滚动市盈率覆盖的宽基（创业板指/科创50 该源未提供，如实缺席）
_VALUATION_INDICES = ('沪深300', '上证50', '中证500', '中证1000')


def fetch_index_valuations(ttl=21600, force=False):
    """宽基指数滚动市盈率与历史分位（乐咕乐股，日频更新）。

    分位 = 当前 PE 在该源全部可得历史（月度，最早自 2005 年）内的排名占比；
    单只失败跳过，全部失败如实不可用。
    """
    if not force:
        cached = cache_get('index_valuations', ttl)
        if cached is not None:
            return cached

    items = []
    for name in _VALUATION_INDICES:
        df = _safe_df_call(ak.stock_index_pe_lg, symbol=name, source_name=f'lg_index_pe_{name}')
        if df is None or df.empty or '滚动市盈率' not in df.columns:
            continue
        pe = pd.to_numeric(df['滚动市盈率'], errors='coerce').dropna()
        dates = pd.to_datetime(df.get('日期'), errors='coerce').dropna()
        if pe.empty or dates.empty:
            continue
        latest = float(pe.iloc[-1])
        items.append({
            'name': name,
            'pe': round(latest, 2),
            'pe_percentile': round(float((pe < latest).mean() * 100), 1),
            'history_count': int(len(pe)),
            'start_date': dates.iloc[0].strftime('%Y-%m-%d'),
            'date': dates.iloc[-1].strftime('%Y-%m-%d'),
        })

    data = {
        'available': bool(items),
        'items': items,
        'message': '' if items else '指数估值暂不可用',
        'meta': _cache_meta(
            'index_valuations',
            ttl,
            'akshare.stock_index_pe_lg (乐咕乐股)',
            bool(items),
            source_data_date=items[-1]['date'] if items else None,
            disclaimer='分位为当前滚动市盈率在该源全部可得历史（月度，自 2005 年）内的排名；创业板指/科创50 该源未提供。',
        ),
    }
    if items:
        cache_set('index_valuations', data)
    return data
