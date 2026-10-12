"""个股两融明细（沪深交易所官方披露，T+1）。

口径：金额统一为亿（上游均为元）；沪市明细不含融券余额列，rq/total 如实降级
为融资口径。按「标的券代码」精确匹配——非两融标的查无此人，如实提示。
"""

from __future__ import annotations

import logging
import akshare as ak

from ._cache import cache_get, _cache_meta, cache_set, stale_or, _to_float
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
    cached = cache_get(key, 21600)
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
        cache_set(key, detail)
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
        cached = cache_get(key, ttl)
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
        return stale_or(key, {
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
    cache_set(key, data)
    return data


# ============================================================
# 个股融资明细按日落库（StockMarginDaily，探测器融资拥挤度用）
# ============================================================

_SOURCE_TAG = {'sse': 'sse_margin_detail', 'szse': 'szse_margin_detail'}


def _to_int(value):
    v = _to_float(value)
    return int(round(v)) if v is not None else None


def _raw_rows_sse(df, date_obj):
    """沪市明细（元）→ {code: {rz, rz_buy, rz_repay}}；信用交易日期与请求日不符整表作废。

    上交所接口对非交易日/未披露日可能回落到别的日期，日期对不上就宁缺毋滥，
    不能把别的交易日的数字写成这一天。
    """
    want = date_obj.strftime('%Y%m%d')
    out = {}
    for _, r in df.iterrows():
        day = str(r.get('信用交易日期', want)).replace('-', '')[:8]
        if day != want:
            return {}
        code = str(r.get('标的证券代码', '')).strip().zfill(6)
        rz = _to_int(r.get('融资余额'))
        if not code.isdigit() or rz is None:
            continue
        out[code] = {'rz': rz, 'rz_buy': _to_int(r.get('融资买入额')),
                     'rz_repay': _to_int(r.get('融资偿还额'))}
    return out


def _raw_rows_szse(df, _date_obj):
    """深市明细（元）→ {code: {rz, rz_buy, rz_repay=None}}；偿还额由落库时推导。"""
    out = {}
    for _, r in df.iterrows():
        code = str(r.get('证券代码', '')).strip().zfill(6)
        rz = _to_int(r.get('融资余额'))
        if not code.isdigit() or rz is None:
            continue
        out[code] = {'rz': rz, 'rz_buy': _to_int(r.get('融资买入额')), 'rz_repay': None}
    return out


_RAW_PARSERS = {'sse': _raw_rows_sse, 'szse': _raw_rows_szse}


def fetch_margin_day_rows(kind, date_obj):
    """某交易所某信用交易日的全市场融资明细（元）；无数据/上游异常返回 None。

    与 _day_detail 同一 akshare 链路（stock_margin_detail_sse/szse），但保留
    融资买入/偿还额并按元取整，不进进程缓存（落库方只拉一次）。
    """
    fn_name, label = _SOURCES[kind]
    try:
        df = getattr(ak, fn_name)(date=date_obj.strftime('%Y%m%d'))
    except Exception as e:  # noqa: BLE001 上游边界：akshare 异常类型不固定
        logger.warning("%s %s 拉取失败: %s", label, date_obj, e)
        return None
    if df is None or df.empty:
        return None
    rows = _RAW_PARSERS[kind](df, date_obj)
    return rows or None


def save_stock_margin_day(kind, date_obj, rows=None):
    """把某交易所某日明细写入 StockMarginDaily（同日同代码覆盖）；返回写入行数。

    rows 缺省时现拉；None/空表示该日该所无数据，返回 0、不写任何行。
    深市偿还额 = 前一信用交易日余额 + 当日买入 − 当日余额（需库里有前一日余额）。
    """
    from django.db import transaction

    from ..models import StockMarginDaily

    if rows is None:
        rows = fetch_margin_day_rows(kind, date_obj)
    if not rows:
        return 0
    prev = {}
    if kind == 'szse':
        prev_date = (
            StockMarginDaily.objects.filter(exchange='szse', trade_date__lt=date_obj)
            .order_by('-trade_date').values_list('trade_date', flat=True).first()
        )
        if prev_date is not None:
            prev = dict(
                StockMarginDaily.objects.filter(exchange='szse', trade_date=prev_date)
                .values_list('code', 'rz')
            )
    objs = []
    for code, r in rows.items():
        repay, derived = r.get('rz_repay'), False
        if kind == 'szse' and code in prev and r.get('rz_buy') is not None:
            repay = prev[code] + r['rz_buy'] - r['rz']
            derived = True
        objs.append(StockMarginDaily(
            code=code, trade_date=date_obj, exchange=kind, rz=r['rz'], rz_buy=r.get('rz_buy'),
            rz_repay=repay, rz_repay_derived=derived, source=_SOURCE_TAG[kind],
        ))
    with transaction.atomic():
        StockMarginDaily.objects.filter(trade_date=date_obj, exchange=kind).delete()
        StockMarginDaily.objects.bulk_create(objs, batch_size=500)
    return len(objs)


def stock_margin_days_present(start, end):
    """[start, end] 内两个交易所都已落库的信用交易日集合（回补/补拉跳过用）。"""
    from ..models import StockMarginDaily

    seen = {}
    for day, kind in (
        StockMarginDaily.objects.filter(trade_date__gte=start, trade_date__lte=end)
        .values_list('trade_date', 'exchange').distinct()
    ):
        seen.setdefault(day, set()).add(kind)
    return {d for d, kinds in seen.items() if kinds >= set(_MARGIN_KINDS)}


def sync_recent_stock_margin(n_days=3, today=None):
    """补拉今天之前最近 n_days 个信用交易日里缺失的个股融资明细（日度任务用）。

    交易所 T+1 早间披露：今天只能拿到上一交易日及更早。顺带检查再往前两天，
    某天上游短暂失败时下一次任务自动补上；已落库的（交易所, 日期）不重复拉。
    按日期升序写入，深市偿还额推导能用上前一日余额。返回 {日期 ISO: {所: 行数}}。
    """
    from datetime import timedelta

    from django.utils import timezone

    from ..models import StockMarginDaily
    from ..trading_calendar import recent_trading_days

    today = today or timezone.localdate()
    days = recent_trading_days(n_days, end_date=today - timedelta(days=1))
    if not days:
        logger.warning("个股两融补拉：交易日历不可用，跳过")
        return {}
    have = set(
        StockMarginDaily.objects.filter(trade_date__gte=days[0], trade_date__lte=days[-1])
        .values_list('trade_date', 'exchange').distinct()
    )
    out = {}
    for day in days:
        for kind in _MARGIN_KINDS:
            if (day, kind) in have:
                continue
            n = save_stock_margin_day(kind, day)
            out.setdefault(day.isoformat(), {})[kind] = n
            if not n:
                logger.warning("个股两融 %s %s 暂无数据（可能尚未披露）", kind, day)
    return out
