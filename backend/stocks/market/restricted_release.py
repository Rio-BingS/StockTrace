"""限售股解禁日程（东方财富数据中心 RPT_LIFT_STAGE，akshare stock_restricted_release_detail_em 同源）。

为什么直接请求数据中心接口而不调 akshare：akshare 的该函数按列位置硬改中文列名，
上游列顺序一变就会把比例写进别的列；这里按字段名取值，同一个接口、同样的字段。

口径：
- FREE_DATE 解禁日；FREE_RATIO 占解禁前流通股比例（小数）；FREE_SHARES_TYPE 类型；
- CURRENT_FREE_SHARES / ABLE_FREE_SHARES 单位万股，LIFT_MARKET_CAP 单位万元，入库换成股、元；
- 同一（代码、解禁日、类型）的多批次合并：数量/市值/比例相加。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import requests

logger = logging.getLogger(__name__)

_URL = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0 Safari/537.36',
    'Referer': 'https://data.eastmoney.com/dxf/detail.html',
}
_COLUMNS = ('SECURITY_CODE,SECURITY_NAME_ABBR,FREE_DATE,FREE_SHARES_TYPE,CURRENT_FREE_SHARES,'
            'ABLE_FREE_SHARES,LIFT_MARKET_CAP,FREE_RATIO,BATCH_HOLDER_NUM')
_PAGE_SIZE = 500
_MAX_PAGES = 200
_RETRIES = 3
SOURCE_TAG = 'em_rpt_lift_stage'

# 每日刷新窗口（自然日）：前 5 天兜住晚到/更正的已发生批次，后 60 天覆盖
# lift_pressure 需要的「T 之后 20 个交易日」并留足余量
REFRESH_BACK_DAYS = 5
REFRESH_AHEAD_DAYS = 60


class ReleaseFetchError(RuntimeError):
    """解禁日程拉取不完整（任一页失败）：调用方不得据此覆盖库里的旧排期。"""


def _num(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # NaN → None


def _get_page(start, end, page):
    params = {
        'sortColumns': 'FREE_DATE,SECURITY_CODE', 'sortTypes': '1,1',
        'pageSize': str(_PAGE_SIZE), 'pageNumber': str(page),
        'reportName': 'RPT_LIFT_STAGE', 'columns': _COLUMNS,
        'source': 'WEB', 'client': 'WEB',
        'filter': f"(FREE_DATE>='{start.isoformat()}')(FREE_DATE<='{end.isoformat()}')",
    }
    err = None
    for _ in range(_RETRIES):
        try:
            r = requests.get(_URL, params=params, headers=_HEADERS, timeout=20)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            err = e
    raise ReleaseFetchError(f'解禁日程 {start}~{end} 第 {page} 页拉取失败: {err}')


def parse_rows(raw_rows):
    """接口行 → 合并后的 {(code, free_date, share_type): dict}。非 6 位代码/无日期的行丢弃。"""
    out = {}
    for r in raw_rows:
        code = str(r.get('SECURITY_CODE') or '').strip()
        raw_date = str(r.get('FREE_DATE') or '')[:10]
        if len(code) != 6 or not code.isdigit():
            continue
        try:
            free_date = date.fromisoformat(raw_date)
        except ValueError:
            continue
        share_type = str(r.get('FREE_SHARES_TYPE') or '').strip()[:100]
        shares = _num(r.get('CURRENT_FREE_SHARES'))
        able = _num(r.get('ABLE_FREE_SHARES'))
        cap = _num(r.get('LIFT_MARKET_CAP'))
        holders = _num(r.get('BATCH_HOLDER_NUM'))
        row = {
            'code': code,
            'name': str(r.get('SECURITY_NAME_ABBR') or '').strip()[:32],
            'free_date': free_date,
            'share_type': share_type,
            'free_shares': shares * 1e4 if shares is not None else None,
            'able_free_shares': able * 1e4 if able is not None else None,
            'free_ratio': _num(r.get('FREE_RATIO')),
            'lift_market_cap': cap * 1e4 if cap is not None else None,
            'holder_num': int(holders) if holders is not None else None,
        }
        key = (code, free_date, share_type)
        if key in out:
            prev = out[key]
            for f in ('free_shares', 'able_free_shares', 'free_ratio', 'lift_market_cap', 'holder_num'):
                if row[f] is not None:
                    prev[f] = (prev[f] or 0) + row[f]
        else:
            out[key] = row
    return out


def fetch_releases(start, end):
    """[start, end]（解禁日，含两端）内的全部解禁批次，合并后返回 dict。

    任一页失败抛 ReleaseFetchError（不返回半截数据）；区间内无批次返回 {}。
    """
    rows, page, pages = [], 1, 1
    while page <= pages and page <= _MAX_PAGES:
        payload = _get_page(start, end, page)
        result = (payload or {}).get('result')
        if not result:
            if page == 1:
                # 东财对空区间返回 result=null（code 9201「返回数据为空」）
                return {}
            raise ReleaseFetchError(f'解禁日程 {start}~{end} 第 {page} 页为空: {str(payload)[:120]}')
        pages = int(result.get('pages') or 1)
        rows.extend(result.get('data') or [])
        page += 1
    return parse_rows(rows)


def replace_window(start, end, releases):
    """用 releases 整段覆盖库里解禁日在 [start, end] 的行；返回写入行数。

    整段覆盖而非逐行 upsert：日程推迟/取消时，旧排期必须从库里消失，否则会在
    原日期继续触发解禁提示。
    """
    from django.db import transaction

    from ..models import StockRestrictedRelease

    objs = [StockRestrictedRelease(source=SOURCE_TAG, **row) for row in releases.values()]
    with transaction.atomic():
        StockRestrictedRelease.objects.filter(free_date__gte=start, free_date__lte=end).delete()
        StockRestrictedRelease.objects.bulk_create(objs, batch_size=500)
    return len(objs)


def refresh_restricted_releases(today=None):
    """每日刷新：覆盖解禁日在 [今天−5, 今天+60] 的日程。返回 (start, end, 行数)。

    拉取不完整时抛 ReleaseFetchError，库里保留旧排期（宁可旧也不可缺）。
    """
    from django.utils import timezone

    today = today or timezone.localdate()
    start = today - timedelta(days=REFRESH_BACK_DAYS)
    end = today + timedelta(days=REFRESH_AHEAD_DAYS)
    releases = fetch_releases(start, end)
    n = replace_window(start, end, releases)
    logger.info("限售解禁日程刷新 %s~%s：%s 行", start, end, n)
    return start, end, n
