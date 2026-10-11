"""国家队 ETF 区间资金流向。

数据源：东方财富 fflow 历史资金流日线（公开接口，未走 akshare——
本机/代理对该域名的 TLS 握手存在间歇性失败，因此用原生 requests
做 https 重试 + http 兜底，并接入源冷却）。

镜像：push2his 为完整历史（约 120 交易日）；push2delay 为可达镜像但
仅当日一根——2026-09 起 push2his 对境外来源被上游掐断，push2delay
作应急兜底（当日值不空白，具体见 fetch_flow_klines）。

边界（如实标注在 meta）：
- push2his 仅提供最近约 120 个交易日的资金流历史，更早无法计算；
- 主力净流入为当日资金快照口径，不代表真实持仓变化；
- ETF 行字段与股票不同：日期、主力、小单、中单、大单、超大单、
  各占比、收盘、涨跌幅（已按 主力=超大单+大单 校验）。
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta

import requests
from django.utils import timezone

from ._cache import _cache_get, _cache_meta, _cache_set, _to_float
from .etf import NATIONAL_TEAM_ETFS

logger = logging.getLogger(__name__)

_FLOW_APIS = ('push2his.eastmoney.com', 'push2delay.eastmoney.com')
# 最近一次成功供数的镜像（仅用于 meta.source 如实标注；线程间竞态最多错标标签）
_FLOW_LAST_HOST = {'host': _FLOW_APIS[0]}
_FLOW_FIELDS = 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65'
_FLOW_UT = 'b2884a393a59ad64002292a3e90d46a5'
_FLOW_HIST_TTL = 1800
_FLOW_REQUEST_TIMEOUT = 12
# 本机/代理对东财 TLS 存在间歇性干扰：https 多次退避重试，http 作最后兜底
_FLOW_ATTEMPTS = (('https', 2.0), ('https', 4.0), ('http', 0.0))
_FLOW_FETCH_INTERVAL = 1.2

# period -> 往回推的自然日数；ytd 为年初至今。1d/3d/5d 按交易日取尾部。
_FLOW_PERIODS = {'1d': 1, '3d': 3, '5d': 5, '1w': 7, '1m': 30, '3m': 90, '6m': 180, 'ytd': None}
_FLOW_TRADING_DAY_PERIODS = ('1d', '3d', '5d')

_NTF_CONSECUTIVE_FAIL_LIMIT = 3


def _flow_source():
    return f'eastmoney.{_FLOW_LAST_HOST["host"]} fflow daykline'


def fetch_flow_klines(secid):
    """东财 fflow 历史日线原始 klines；push2his 失败时退 push2delay（仅当日一根）。

    secid 形如 '1.510300'（沪 ETF）、'0.159915'（深 ETF）、'1.000001'（上证指数/大盘）。
    """
    params = {
        'lmt': '0', 'klt': '101', 'secid': secid,
        'fields1': 'f1,f2,f3,f7',
        'fields2': _FLOW_FIELDS,
        'ut': _FLOW_UT,
    }
    errors = []
    for host in _FLOW_APIS:
        last = None
        for scheme, backoff in _FLOW_ATTEMPTS:
            try:
                resp = requests.get(
                    f'{scheme}://{host}/api/qt/stock/fflow/daykline/get',
                    params=params, timeout=_FLOW_REQUEST_TIMEOUT,
                )
                klines = (resp.json().get('data') or {}).get('klines') or []
                if klines:
                    _FLOW_LAST_HOST['host'] = host
                    return klines
                last = RuntimeError('empty klines')
            except Exception as e:  # noqa: BLE001  网络/限流逐级降级
                last = e
            if backoff:
                time.sleep(backoff)
        errors.append(f'{host}: {last}')
    raise RuntimeError('; '.join(errors))


def _fetch_klines(code):
    return fetch_flow_klines(('1' if code.startswith('5') else '0') + '.' + code)


def parse_flow_kline(row):
    """资金流行 -> dict；字段不足或非数值时返回 None。"""
    parts = row.split(',')
    if len(parts) < 13:
        return None
    return {
        'date': parts[0][:10],
        'main_net': _to_float(parts[1]),
        'small_net': _to_float(parts[2]),
        'mid_net': _to_float(parts[3]),
        'large_net': _to_float(parts[4]),
        'super_net': _to_float(parts[5]),
        'close': _to_float(parts[11]),
        'change_pct': _to_float(parts[12]),
    }


# 兼容旧名
_parse_kline = parse_flow_kline


def flow_history(code, ttl=_FLOW_HIST_TTL):
    """单只 ETF 的历史每日资金流（进程内缓存，日历感知保鲜）。"""
    cache_key = f'em_fflow_hist_{code}'
    cached = _cache_get(cache_key, ttl)
    if cached is not None:
        return cached
    rows = [r for r in (_parse_kline(k) for k in _fetch_klines(code)) if r]
    if rows:
        _cache_set(cache_key, rows)
    return rows


def get_national_team_flow(period='3m', start=None, end=None, ttl=1800):
    """国家队 ETF 在指定区间的主力净流入聚合。

    period 1d/3d/5d 按交易日取尾部，1w/1m/3m/6m 按自然日回推，ytd 年初至今；
    也可传 start/end（YYYY-MM-DD）自定义区间。上游深度约 120 个交易日，
    起点早于覆盖范围时如实截断并在 note 标注。
    """
    custom = bool(start or end)
    if custom:
        try:
            start_d = date.fromisoformat(start) if start else None
            end_d = date.fromisoformat(end) if end else None
        except (TypeError, ValueError):
            raise ValueError('start/end 必须是 YYYY-MM-DD')
        if start_d and end_d and start_d > end_d:
            raise ValueError('start 不能晚于 end')
        if end_d and end_d > timezone.localdate():
            raise ValueError('end 不能晚于今天')
        if start_d and (end_d or timezone.localdate()) - start_d > timedelta(days=400):
            raise ValueError('自定义区间最长约 400 天')
        cache_key = f'ntf_flow_{start or "begin"}_{end or "now"}'
        window_start = (start_d or (end_d or timezone.localdate()) - timedelta(days=90)).isoformat()
        window_end = (end_d or timezone.localdate()).isoformat()
        trading_tail = None
    else:
        if period not in _FLOW_PERIODS:
            raise ValueError('period 必须是 1d、3d、5d、1w、1m、3m、6m 或 ytd')
        cache_key = f'ntf_flow_{period}'
        cached = _cache_get(cache_key, ttl)
        if cached is not None:
            return cached
        days = _FLOW_PERIODS[period]
        end_d = timezone.localdate()
        start_d = (end_d - timedelta(days=days)) if days else date(end_d.year, 1, 1)
        window_start = start_d.isoformat()
        window_end = end_d.isoformat()
        trading_tail = days if period in _FLOW_TRADING_DAY_PERIODS else None

    items = []
    daily_total = {}
    failed_codes = []
    fetched = 0
    consecutive_fail = 0
    coverage_start = None

    # 探针：整条线路不可达时立即返回，避免 18 连发空耗
    try:
        flow_history(NATIONAL_TEAM_ETFS[0][0])
    except Exception as e:  # noqa: BLE001
        logger.warning('国家队资金流探针失败，跳过本轮: %s', e)
        return _unavailable_payload(cache_key, ttl, 'custom' if custom else period, window_start, str(e))

    for code, name in NATIONAL_TEAM_ETFS:
        if consecutive_fail >= _NTF_CONSECUTIVE_FAIL_LIMIT:
            logger.error('国家队资金流连续失败 %d 只，提前终止', consecutive_fail)
            break
        try:
            rows = flow_history(code)
        except Exception as e:  # noqa: BLE001  单只失败不阻断整体
            failed_codes.append(code)
            consecutive_fail += 1
            logger.warning('资金流历史 %s 获取失败: %s', code, e)
            continue
        consecutive_fail = 0
        fetched += 1
        if rows:
            coverage_start = min(coverage_start or rows[0]['date'], rows[0]['date'])

        if trading_tail:
            window = rows[-trading_tail:]
        else:
            window = [r for r in rows if window_start <= r['date'] <= window_end]
        total_main = sum(r['main_net'] or 0 for r in window)
        for r in window:
            daily_total[r['date']] = daily_total.get(r['date'], 0.0) + (r['main_net'] or 0)
        closes = [r['close'] for r in window if r.get('close') is not None]
        window_change = (
            round((closes[-1] / closes[0] - 1) * 100, 3)
            if len(closes) >= 2 and closes[0] else None
        )
        items.append({
            'code': code,
            'name': name,
            'available': bool(window),
            'days': len(window),
            'total_main_net': total_main if window else None,
            'up_days': sum(1 for r in window if (r['main_net'] or 0) > 0),
            'down_days': sum(1 for r in window if (r['main_net'] or 0) < 0),
            'last_close': closes[-1] if closes else None,
            'window_change_pct': window_change,
        })
        time.sleep(_FLOW_FETCH_INTERVAL)

    items.sort(key=lambda x: (x['total_main_net'] or 0), reverse=True)
    daily_series = [
        {'date': d, 'main_net': round(v, 2)}
        for d, v in sorted(daily_total.items())
    ]
    valued = [i for i in items if i['available']]
    grand_total = sum(i['total_main_net'] or 0 for i in valued)
    end_date = daily_series[-1]['date'] if daily_series else None

    data = {
        'period': 'custom' if custom else period,
        'start': window_start,
        'end_requested': window_end if custom else None,
        'coverage_start': coverage_start,
        'end': end_date,
        'truncated': bool(coverage_start and window_start < coverage_start),
        'available': fetched > 0,
        'items': items,
        'total_daily': daily_series,
        'summary': {
            'etf_count': len(NATIONAL_TEAM_ETFS),
            'fetched_count': fetched,
            'available_count': len(valued),
            'total_main_net': grand_total if valued else None,
            'inflow_count': sum(1 for i in valued if (i['total_main_net'] or 0) > 0),
            'outflow_count': sum(1 for i in valued if (i['total_main_net'] or 0) < 0),
        },
        'failed_codes': failed_codes,
        'message': '' if fetched else '历史资金流暂不可用（上游限流或网络异常）',
        'note': (
            f'区间起点早于上游覆盖范围，实际自 {coverage_start} 起计算'
            if coverage_start and window_start < coverage_start else ''
        ),
        'meta': _cache_meta(
            cache_key, ttl,
            _flow_source(),
            fetched > 0,
            source_data_date=end_date,
            disclaimer=(
                '历史主力净流入来自东方财富，上游仅提供最近约 120 个交易日，'
                '更早区间无法计算；为当日资金快照口径，不代表真实持仓变化。'
            ),
        ),
    }
    # 只缓存完整结果；部分失败时下次请求仅重试失败标的（成功的走单只缓存）
    if fetched == len(NATIONAL_TEAM_ETFS):
        _cache_set(cache_key, data)
    return data


def _unavailable_payload(cache_key, ttl, period, start, reason):
    """上游整条线路不可达时的快速失败响应（不缓存，便于网络恢复后重试）。"""
    return {
        'period': period,
        'start': start,
        'coverage_start': None,
        'end': None,
        'available': False,
        'items': [],
        'total_daily': [],
        'summary': {
            'etf_count': len(NATIONAL_TEAM_ETFS),
            'fetched_count': 0,
            'available_count': 0,
            'total_main_net': None,
            'inflow_count': 0,
            'outflow_count': 0,
        },
        'failed_codes': [],
        'message': f'东财资金流线路当前不可达（{reason}），稍后可重试',
        'meta': _cache_meta(
            cache_key, ttl, _flow_source(), False,
            disclaimer='历史主力净流入来自东方财富，上游仅提供最近约 120 个交易日。',
        ),
    }
