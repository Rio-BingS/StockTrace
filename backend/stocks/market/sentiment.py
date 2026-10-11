"""涨跌停股池情绪与沪深两融余额（东财股池 + 交易所官方披露）。"""

from __future__ import annotations

import logging
from datetime import date as _date, timedelta

import akshare as ak
import pandas as pd
from django.utils import timezone

from ._cache import _cache_get, _cache_meta, _cache_set, _stale_or, _to_float
from ._sources import _safe_df_call
from .. import trading_calendar

logger = logging.getLogger(__name__)

_ZT_TOP_N = 5
_MARGIN_LOOKBACK_DAYS = 21


def _recent_day_candidates(n=4, today=None):
    """「今天优先、其后最近交易日」候选日（薄包装，实现见 trading_calendar，ARC-02）"""
    return trading_calendar.recent_day_candidates(n, today)


def _prev_trading_day(d):
    """前一交易日（薄包装，实现见 trading_calendar，ARC-02）"""
    return trading_calendar.prev_trading_day(d)


def _zt_top(df):
    """连板数降序（同板按成交额）取前 N 只。"""
    work = pd.DataFrame({
        'lb': pd.to_numeric(df.get('连板数'), errors='coerce'),
        'amt': pd.to_numeric(df.get('成交额'), errors='coerce'),
        'pct': pd.to_numeric(df.get('涨跌幅'), errors='coerce'),
        'price': pd.to_numeric(df.get('最新价'), errors='coerce'),
        'code': df.get('代码').astype(str) if '代码' in df.columns else '',
        'name': df.get('名称').astype(str) if '名称' in df.columns else '',
        'industry': df.get('所属行业').astype(str) if '所属行业' in df.columns else '',
    })
    work = work.sort_values(['lb', 'amt'], ascending=False, na_position='last').head(_ZT_TOP_N)
    return [
        {
            'code': r['code'],
            'name': r['name'],
            'lb': int(r['lb']) if pd.notna(r['lb']) else 1,
            'industry': r['industry'] or '',
            'price': _to_float(r['price']),
            'pct': _to_float(r['pct']),
        }
        for _, r in work.iterrows()
    ]


def fetch_zt_sentiment(ttl=600, force=False):
    """涨停/跌停/炸板股池情绪（东财股池，盘中为实时池口径）。

    今天无池数据（非交易日/盘前/上游异常）时回退最近一个有数据的交易日，
    并在 message 中如实标注；封板率 = 涨停 / (涨停 + 炸板)。
    """
    if not force:
        cached = _cache_get('zt_sentiment', ttl)
        if cached is not None:
            return cached

    today = timezone.localdate()
    chosen = None
    zt_df = dt_df = zb_df = None
    for d in _recent_day_candidates(4, today):
        ds = d.strftime('%Y%m%d')
        zt = _safe_df_call(ak.stock_zt_pool_em, date=ds, source_name='em_zt_pool')
        if zt is None:
            continue
        chosen = d
        zt_df = zt
        # 跌停池/炸板池当日为空是合法结果（0 只），不能当作源失败（DAT-10）
        dt_df = _safe_df_call(ak.stock_zt_pool_dtgc_em, date=ds, source_name='em_zt_dt_pool',
                              empty_ok=True)
        zb_df = _safe_df_call(ak.stock_zt_pool_zbgc_em, date=ds, source_name='em_zt_zb_pool',
                              empty_ok=True)
        break

    if chosen is None:
        return _stale_or('zt_sentiment', {
            'available': False,
            'message': '涨停池数据暂不可用（非交易时段或上游异常），稍后再试',
            'meta': _cache_meta('zt_sentiment', ttl, '东方财富涨停/跌停/炸板股池', False),
        })

    zt_count = int(len(zt_df))
    dt_count = int(len(dt_df)) if dt_df is not None else None
    zb_count = int(len(zb_df)) if zb_df is not None else None
    lb = pd.to_numeric(zt_df.get('连板数'), errors='coerce')
    has_lb = lb.notna().any()
    seal_rate = None
    if zb_count is not None and (zt_count + zb_count) > 0:
        seal_rate = round(zt_count / (zt_count + zb_count) * 100, 1)

    data = {
        'available': True,
        'date': chosen.isoformat(),
        'is_live': bool(chosen == today),
        'zt_count': zt_count,
        'dt_count': dt_count,
        'zb_count': zb_count,
        'seal_rate': seal_rate,
        'lb_count': int((lb >= 2).sum()) if has_lb else None,
        'max_lb': int(lb.max()) if has_lb else None,
        'top': _zt_top(zt_df),
        'message': '' if chosen == today else f'今日暂无池数据，展示最近交易日 {chosen.isoformat()} 收盘口径',
        'meta': _cache_meta(
            'zt_sentiment', ttl, '东方财富涨停/跌停/炸板股池', True,
            source_data_date=chosen.isoformat(),
            disclaimer='盘中共为实时池口径，收盘后为全天口径；封板率 = 涨停/(涨停+炸板)。',
        ),
    }
    _cache_set('zt_sentiment', data)
    return data


def _sh_margin_rows():
    """沪市两融近况：{yyyymmdd: {rz, rq, total}}（元 → 亿）。"""
    df = _safe_df_call(
        ak.stock_margin_sse,
        start_date=(timezone.localdate() - timedelta(days=_MARGIN_LOOKBACK_DAYS)).strftime('%Y%m%d'),
        end_date=timezone.localdate().strftime('%Y%m%d'),
        source_name='sse_margin',
    )
    if df is None or df.empty or '信用交易日期' not in df.columns or '融资融券余额' not in df.columns:
        return {}
    rows = {}
    for _, r in df.iterrows():
        d = str(r.get('信用交易日期')).replace('-', '')[:8]
        total = _to_float(r.get('融资融券余额'))
        if len(d) != 8 or total is None:
            continue
        rz = _to_float(r.get('融资余额'))
        rq = _to_float(r.get('融券余量金额'))
        rows[d] = {
            'rz': round(rz / 1e8, 2) if rz is not None else None,
            'rq': round(rq / 1e8, 2) if rq is not None else None,
            'total': round(total / 1e8, 2),
        }
    return rows


def _sz_margin_row(date_obj):
    """深市两融汇总（单日，亿元）；深交所对未披露日期会直接抛错，
    按日回退探测属预期状态，因此不走 _safe_df_call 冷却统计。"""
    try:
        df = ak.stock_margin_szse(date=date_obj.strftime('%Y%m%d'))
    except Exception:
        return None
    if df is None or df.empty or '融资融券余额' not in df.columns:
        return None
    total = _to_float(df['融资融券余额'].iloc[0])
    if total is None:
        return None
    row = {'total': round(total, 2)}
    for col, key in (('融资余额', 'rz'), ('融券余额', 'rq')):
        row[key] = _to_float(df[col].iloc[0]) if col in df.columns else None
    return row


def _margin_payload(date_obj, scope, sh_today, sz_today, sh_prev, sz_prev):
    """组装两融卡片 payload；日变化仅用同口径前一日（沪深合计需两市都齐）。"""
    sz_today = sz_today or {}
    total = round(sh_today['total'] + (sz_today.get('total') or 0), 2)
    rz = round((sh_today['rz'] or 0) + (sz_today.get('rz') or 0), 2)
    data = {
        'available': True,
        'date': date_obj.isoformat(),
        'scope': scope,
        'total': total,
        'rz': rz,
        'rq': round(total - rz, 2),
        'sh_total': sh_today['total'],
        'sz_total': sz_today.get('total'),
        'chg_1d': None,
        'chg_pct_1d': None,
    }
    prev_total = None
    if scope == 'sh_sz':
        if sh_prev is not None and sz_prev is not None:
            prev_total = round(sh_prev['total'] + (sz_prev.get('total') or 0), 2)
    elif sh_prev is not None:
        prev_total = sh_prev['total']
    if prev_total:
        chg = round(total - prev_total, 2)
        data['chg_1d'] = chg
        data['chg_pct_1d'] = round(chg / prev_total * 100, 2)
    data['meta'] = _cache_meta(
        'margin_balance', ttl=21600, source='上交所/深交所两融汇总披露', available=True,
        source_data_date=date_obj.isoformat(),
        disclaimer='两融余额 T+1 披露；合计为沪深同日口径（不含北交所）。',
    )
    return data


def fetch_margin_balance(ttl=21600, force=False):
    """沪深两融余额（交易所官方披露，T+1），合计取沪深同日口径。

    深市披露晚于沪市：以深市最新可得交易日为基准，沪市取同日值；
    深市整体不可用时降级为沪市口径并如实标注。
    """
    if not force:
        cached = _cache_get('margin_balance', ttl)
        if cached is not None:
            return cached

    sh = _sh_margin_rows()
    base = None
    for d in _recent_day_candidates(3):
        row = _sz_margin_row(d)
        if row:
            base = (d, row)
            break

    def _d8(d):
        return d.strftime('%Y%m%d')

    if base and _d8(base[0]) in sh:
        d, sz = base
        prev_d = _prev_trading_day(d)
        data = _margin_payload(
            d, 'sh_sz', sh[_d8(d)], sz, sh.get(_d8(prev_d)), _sz_margin_row(prev_d),
        )
        if data['chg_1d'] is None:
            data['message'] = '前一日沪深数据未齐，未计算日变化'
    elif sh:
        dates = sorted(sh)
        latest, prev = dates[-1], (dates[-2] if len(dates) >= 2 else None)
        latest_d = _date(int(latest[:4]), int(latest[4:6]), int(latest[6:]))
        sh_prev = sh[prev] if prev else None
        data = _margin_payload(latest_d, 'sh_only', sh[latest], None, sh_prev, None)
        data['message'] = '深市两融暂未披露，当前为沪市口径'
        if data['chg_1d'] is None:
            data['message'] += '；前一日数据缺失，未计算日变化'
    else:
        return _stale_or('margin_balance', {
            'available': False,
            'message': '两融余额数据暂不可用（交易所披露延迟或上游异常）',
            'meta': _cache_meta('margin_balance', ttl, '上交所/深交所两融汇总披露', False),
        })

    _cache_set('margin_balance', data)
    return data
