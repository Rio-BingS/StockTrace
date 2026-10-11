"""个股两融明细（沪深交易所官方披露，T+1）。

口径：金额统一为亿（上游均为元）；沪市明细不含融券余额列，rq/total 如实降级
为融资口径。按「标的券代码」精确匹配——非两融标的查无此人，如实提示。
"""

from __future__ import annotations

import logging
import akshare as ak

from ._cache import _cache_get, _cache_meta, _cache_set, _stale_or, _to_float
from ..trading_calendar import prev_trading_day as _prev_trading_day
from ..trading_calendar import recent_day_candidates as _recent_day_candidates

logger = logging.getLogger(__name__)

_MARGIN_KINDS = ('sse', 'szse')


def _kind_for_code(code):
    if code.startswith('6'):
        return 'sse'
    if code.startswith(('0', '3')):
        return 'szse'
    return None


def _parse_sse(df):
    """沪市明细行（元）→ {code: {name, rz, rq, total}}；沪市无融券余额列。"""
    out = {}
    for _, r in df.iterrows():
        code = str(r.get('标的证券代码', '')).zfill(6)
        rz = _to_float(r.get('融资余额'))
        if not code or rz is None:
            continue
        out[code] = {
            'name': str(r.get('标的证券简称', '')).strip(),
            'rz': round(rz / 1e8, 4),
            'rq': None,
            'total': round(rz / 1e8, 4),
        }
    return out


def _parse_szse(df):
    """深市明细行（元）→ {code: {...}}；融资融券余额 = 融资余额 + 融券余额。"""
    out = {}
    for _, r in df.iterrows():
        code = str(r.get('证券代码', '')).zfill(6)
        rz = _to_float(r.get('融资余额'))
        if not code or rz is None:
            continue
        rq = _to_float(r.get('融券余额'))
        total = _to_float(r.get('融资融券余额'))
        out[code] = {
            'name': str(r.get('证券简称', '')).strip(),
            'rz': round(rz / 1e8, 4),
            'rq': round(rq / 1e8, 4) if rq is not None else None,
            'total': round(total / 1e8, 4) if total is not None else round(rz / 1e8, 4),
        }
    return out


_PARSERS = {'sse': _parse_sse, 'szse': _parse_szse}
# 函数按名字在调用时解析（便于测试替换）；名字存在于全部支持的 akshare 版本
_SOURCES = {'sse': ('stock_margin_detail_sse', '上交所两融明细'),
            'szse': ('stock_margin_detail_szse', '深交所两融明细')}


def _day_detail(kind, date_obj):
    """某交易所某日全市场明细（进程缓存 key：margin_detail_{kind}_{date}）。"""
    key = f'margin_detail_{kind}_{date_obj.strftime("%Y%m%d")}'
    cached = _cache_get(key, 21600)
    if cached is not None:
        return cached
    fn_name, label = _SOURCES[kind]
    try:
        df = getattr(ak, fn_name)(date=date_obj.strftime('%Y%m%d'))
    except Exception as e:  # noqa: BLE001 上游边界：akshare 异常类型不固定
        logger.warning("%s %s 拉取失败: %s", label, date_obj, e)
        return None
    if df is None or df.empty:
        return None
    detail = _PARSERS[kind](df)
    if detail:
        _cache_set(key, detail)
    return detail


def fetch_stock_margin(code, ttl=21600, force=False):
    """个股两融余额（交易所官方披露，T+1）：最新值 + 1 日变化。

    非两融标的/未披露/上游异常均如实降级；沪市明细无融券列，rq 如实 None。
    """
    code = code.zfill(6)
    kind = _kind_for_code(code)
    if kind is None:
        return {
            'available': False,
            'message': '仅沪深 A 股有两融明细',
            'meta': _cache_meta(f'stock_margin_{code}', ttl, '上交所/深交所两融明细', False),
        }

    key = f'stock_margin_{code}'
    if not force:
        cached = _cache_get(key, ttl)
        if cached is not None:
            return cached

    def detail_fn(d):
        return _day_detail(kind, d)

    latest = None
    for d in _recent_day_candidates(4):
        detail = detail_fn(d)
        if detail and code in detail:
            latest = (d, detail[code])
            break
    if latest is None:
        return _stale_or(key, {
            'available': False,
            'message': '未查到该股两融明细（可能非两融标的或披露延迟）',
            'meta': _cache_meta(key, ttl, '上交所/深交所两融明细', False),
        })

    d, row = latest
    data = {
        'available': True,
        'date': d.isoformat(),
        'code': code,
        'name': row['name'],
        'rz': row['rz'],
        'rq': row['rq'],
        'total': row['total'],
        'chg_1d': None,
        'chg_pct_1d': None,
        'meta': _cache_meta(
            key, ttl, '上交所/深交所两融明细披露', True,
            source_data_date=d.isoformat(),
            disclaimer='两融明细 T+1 披露；沪市无融券余额列，rq/total 为融资口径。'
            if kind == 'sse' else '两融明细 T+1 披露。',
        ),
    }
    try:
        prev = detail_fn(_prev_trading_day(d)) or {}
        prev_row = prev.get(code)
        if prev_row and prev_row.get('rz') is not None and row['rz'] is not None:
            chg = round(row['rz'] - prev_row['rz'], 4)
            data['chg_1d'] = chg
            data['chg_pct_1d'] = round(chg / prev_row['rz'] * 100, 2) if prev_row['rz'] else None
    except Exception as e:  # noqa: BLE001 边界：前一日明细只影响 chg_1d，其拉取/解析异常类型不固定
        logger.warning("个股两融前一日明细获取失败（%s）: %s", code, e)
    _cache_set(key, data)
    return data
