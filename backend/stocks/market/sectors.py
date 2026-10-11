"""板块资金轮动（行业 / 概念，当日横截面）。"""

from __future__ import annotations

import logging
import re
import threading
import time
from io import StringIO
from pathlib import Path

import akshare as ak
import pandas as pd
import requests

from ..common.envelope import envelope
from ..errors import BadRequest
from ._cache import _cache_get, _cache_meta, _cache_set, _now_str, _to_float
from ._query import _paginate, _sort_items
from ._sources import _safe_df_call

logger = logging.getLogger(__name__)

_SECTOR_SORT_FIELDS = {
    'net': 'net',
    'inflow': 'inflow',
    'outflow': 'outflow',
    'change_pct': 'change_pct',
    'leader_pct': 'leader_pct',
}

# 多日（5d/10d/20d）轮动口径固定为「本站日度快照（同花顺当日净额）累加」，窗口
# 不齐就如实不可用（DAT-15）：不再退回东财原生 5日/10日排行——那是另一套主力
# 口径和行业名单，随快照积累程度来回切换会让同一页面前后不可比。
# 下面的东财排行指标只保留给 fetch_*_fund_flow(period=...) 的直接调用方。
_SECTOR_RANK_INDICATORS = {'5d': '5日', '10d': '10日'}
_SECTOR_PERIODS = ('day', '5d', '10d', '20d')
_SNAPSHOT_PERIOD_DAYS = {'5d': 5, '10d': 10, '20d': 20}
# 当日横截面实际来源：自抓同花顺资金流页面（非 akshare 封装）
_THS_DAY_SOURCE = {
    'industry': '同花顺 data.10jqka.com.cn/funds/hyzjl（自抓）',
    'concept': '同花顺 data.10jqka.com.cn/funds/gnzjl（自抓）',
}


def _parse_sector_rank_table(df):
    """解析 stock_sector_fund_flow_rank 的 clist 形状（列带指标前缀，单位为元）。"""
    if df is None or df.empty:
        return []
    name_col = next((c for c in ('行业', '板块名', '名称') if c in df.columns), None)
    net_col = next((c for c in df.columns if str(c).endswith('主力净流入-净额')), None)
    if name_col is None or net_col is None:
        return []
    pct_col = next((c for c in df.columns if str(c).endswith('涨跌幅')), None)

    items = []
    for _, row in df.iterrows():
        net = _to_float(row.get(net_col))
        if net is None:
            continue
        items.append({
            'name': str(row.get(name_col, '')).strip(),
            'net': net,
            'inflow': None,
            'outflow': None,
            'change_pct': _to_float(row.get(pct_col)) if pct_col else None,
            'index_value': None,
            'company_count': None,
            'leader': None,
            'leader_pct': None,
        })

    by_name = {}
    for item in items:
        if not item['name']:
            continue
        previous = by_name.get(item['name'])
        if previous is None or abs(item['net']) > abs(previous['net']):
            by_name[item['name']] = item
    return list(by_name.values())


def _parse_fund_flow_table(df, name_col_candidates):
    if df is None or df.empty:
        return []

    name_col = next((c for c in name_col_candidates if c in df.columns), None)
    if name_col is None:
        name_col = df.columns[1] if len(df.columns) > 1 else df.columns[0]

    net_col = '净额' if '净额' in df.columns else None
    if net_col is None:
        return []

    in_col = '流入资金' if '流入资金' in df.columns else None
    out_col = '流出资金' if '流出资金' in df.columns else None
    pct_col = next(
        (c for c in ('行业-涨跌幅', '概念-涨跌幅', '涨跌幅') if c in df.columns), None,
    )
    index_col = next((c for c in ('行业指数', '概念指数') if c in df.columns), None)
    count_col = '公司家数' if '公司家数' in df.columns else None
    leader_col = '领涨股' if '领涨股' in df.columns else None
    leader_pct_col = '领涨股-涨跌幅' if '领涨股-涨跌幅' in df.columns else None

    items = []
    for _, row in df.iterrows():
        net = _to_float(row.get(net_col))
        if net is None:
            continue
        # AkShare 板块资金接口的金额单位为亿元，API 对外统一为元。
        items.append({
            'name': str(row.get(name_col, '')).strip(),
            'net': net * 1e8,
            'inflow': (_to_float(row.get(in_col)) * 1e8) if in_col and _to_float(row.get(in_col)) is not None else None,
            'outflow': (_to_float(row.get(out_col)) * 1e8) if out_col and _to_float(row.get(out_col)) is not None else None,
            'change_pct': _to_float(row.get(pct_col)) if pct_col else None,
            'index_value': _to_float(row.get(index_col)) if index_col else None,
            'company_count': int(row[count_col]) if count_col and pd.notna(row.get(count_col)) else None,
            'leader': str(row.get(leader_col, '')).strip() if leader_col else None,
            'leader_pct': _to_float(row.get(leader_pct_col)) if leader_pct_col else None,
        })

    # 上游横截面偶发同名记录；保留绝对净额较大的项，避免重复计入汇总和前端 key 冲突。
    by_name = {}
    for item in items:
        if not item['name']:
            continue
        previous = by_name.get(item['name'])
        if previous is None or abs(item['net']) > abs(previous['net']):
            by_name[item['name']] = item
    if len(by_name) != len(items):
        logger.warning(f"板块资金上游存在 {len(items) - len(by_name)} 条同名/空名记录，已规范化去重")
    return list(by_name.values())


# 同花顺板块资金流：东财 push2 clist 板块路径 2026-09 起对境外 502，
# 同花顺为独立第二源。akshare 1.18.x 同名封装对现行 11 列页面按旧列数
# 解析会报错，故自行抓取解析（列与页面一致，单位亿元）。
_THS_BOARD_BASES = {
    'industry': 'http://data.10jqka.com.cn/funds/hyzjl/',
    'concept': 'http://data.10jqka.com.cn/funds/gnzjl/',
}
_THS_COLUMNS = {
    'industry': ['序号', '行业', '行业指数', '行业-涨跌幅', '流入资金', '流出资金',
                 '净额', '公司家数', '领涨股', '领涨股-涨跌幅', '当前价'],
    'concept': ['序号', '概念', '概念指数', '概念-涨跌幅', '流入资金', '流出资金',
                '净额', '公司家数', '领涨股', '领涨股-涨跌幅', '当前价'],
}
_THS_MAX_PAGES = 50


# ths.js 已 vendoring 到仓库（DAT-17）：不再依赖 akshare 私有函数 _get_file_content_ths，
# 来源与更新方式见 vendor/README.md
_THS_JS_PATH = Path(__file__).resolve().parent / 'vendor' / 'ths.js'
# hexin-v 按分钟复用：每次都起 MiniRacer 执行 40KB 脚本，在 1H2G 上开销不小
_THS_TOKEN_TTL = 60
_ths_token = {'v': None, 'at': 0.0}
_ths_token_lock = threading.Lock()
# 分页请求间隔（秒）：连续翻 50 页容易触发同花顺反爬封禁
_THS_PAGE_INTERVAL = 0.5
# 翻页排序键（DAT-17）：原先按涨跌幅（tradezdf）排序，盘中每翻一页排名都在变，
# 收盘后涨跌幅相同的板块之间顺序也不固定，会出现跨页重复一行、同时漏掉一行。
# 改按板块名（tradename，页面表头自带的排序字段）升序：名称当日不变且唯一，
# 翻页结果稳定。2026-10-11 实测概念板块 386 行：按涨跌幅翻页只有 385 个不同
# 板块，按名称翻页 386 个全齐。下游展示按 sort 参数重新排序，不依赖这里的顺序。
_THS_SORT_FIELD = 'tradename'
_THS_SORT_ORDER = 'asc'


def _ths_hexin_v():
    """生成（或复用一分钟内的）同花顺 hexin-v token。"""
    with _ths_token_lock:
        now = time.time()
        if _ths_token['v'] and now - _ths_token['at'] < _THS_TOKEN_TTL:
            return _ths_token['v']
        import py_mini_racer

        js_code = py_mini_racer.MiniRacer()
        js_code.eval(_THS_JS_PATH.read_text(encoding='utf-8'))
        _ths_token['v'] = js_code.call('v')
        _ths_token['at'] = now
        return _ths_token['v']


def _ths_token_headers(referer):
    """同花顺请求头（含 hexin-v 反爬 token）。"""
    return {
        'Accept': 'text/html, */*; q=0.01',
        'Accept-Encoding': 'gzip, deflate',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'hexin-v': _ths_hexin_v(),
        'Referer': referer,
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/90.0.4430.85 Safari/537.36'
        ),
        'X-Requested-With': 'XMLHttpRequest',
    }


def _ths_fund_flow(board):
    """抓取同花顺行业/概念资金流当日全量分页，返回与 akshare 输出同构的 DataFrame。"""
    base = _THS_BOARD_BASES[board]
    names = _THS_COLUMNS[board]
    headers = _ths_token_headers(base)

    def _page_html(page):
        url = f'{base}field/{_THS_SORT_FIELD}/order/{_THS_SORT_ORDER}/page/{page}/ajax/1/free/1/'
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        return r.text

    def _parse(html, page):
        tables = pd.read_html(StringIO(html))
        if not tables:
            raise RuntimeError(f'同花顺第 {page} 页无表格')
        df = tables[0]
        if len(df.columns) != len(names):
            raise RuntimeError(
                f'同花顺第 {page} 页列数 {len(df.columns)} 与预期 {len(names)} 不符（页面改版）'
            )
        df.columns = names
        return df

    html1 = _page_html(1)
    frames = [_parse(html1, 1)]
    m = re.search(r'class="page_info"[^>]*>\s*\d+/(\d+)', html1)
    page_num = min(int(m.group(1)) if m else 1, _THS_MAX_PAGES)
    for page in range(2, page_num + 1):
        time.sleep(_THS_PAGE_INTERVAL)
        frames.append(_parse(_page_html(page), page))
    _check_ths_pages(frames, board)
    df = pd.concat(frames, ignore_index=True)
    # 翻页重叠兜底（DAT-17）：同名行只保留第一次出现的；出现重叠说明翻页期间
    # 排序变了，通常还伴随等量漏行，记 warning 便于发现
    name_col = names[1]
    dup = df.duplicated(subset=[name_col], keep='first')
    if dup.any():
        logger.warning(
            f'同花顺{board}资金流翻页出现 {int(dup.sum())} 行重复（已去重），可能有等量漏行'
        )
        df = df.loc[~dup].reset_index(drop=True)
    if '序号' in df.columns:
        df = df.drop(columns=['序号'])
    # 页面涨跌幅带 % 号（akshare 原封装同样先 strip 再返回）
    for col in df.columns:
        if str(col).endswith('涨跌幅'):
            df[col] = df[col].astype(str).str.strip('%')
    return df


def _check_ths_pages(frames, board):
    """非末页行数少于首页时记 warning：页面每页固定条数，中间页变短说明有行没取到。"""
    if len(frames) < 2:
        return
    per_page = len(frames[0])
    short = [i + 1 for i, f in enumerate(frames[:-1]) if len(f) < per_page]
    if short:
        logger.warning(f'同花顺{board}资金流第 {short} 页行数少于首页 {per_page} 行，疑似漏行')


def _ths_fund_flow_df(board, source_name):
    """带源冷却的同花顺板块资金流抓取；失败计入冷却，避免高频重试（走统一 failover，ARC-03）。"""
    return _safe_df_call(_ths_fund_flow, board, source_name=source_name)


def fetch_concept_fund_flow(period='day', ttl=180):
    cache_key = f'concept_ff_{period}'
    cached = _cache_get(cache_key, ttl)
    if cached is not None:
        return cached
    if period == 'day':
        df = _ths_fund_flow_df('concept', 'ths_fund_flow_concept')
        data = _parse_fund_flow_table(df, ['概念', '行业'])
    else:
        indicator = _SECTOR_RANK_INDICATORS[period]
        df = _safe_df_call(
            ak.stock_sector_fund_flow_rank, indicator=indicator,
            sector_type='概念资金流', source_name=f'em_sector_rank_concept_{period}',
        )
        data = _parse_sector_rank_table(df)
    _cache_set(cache_key, data)
    return data


def fetch_industry_fund_flow(period='day', ttl=180):
    cache_key = f'industry_ff_{period}'
    cached = _cache_get(cache_key, ttl)
    if cached is not None:
        return cached
    if period == 'day':
        df = _ths_fund_flow_df('industry', 'ths_fund_flow_industry')
        data = _parse_fund_flow_table(df, ['行业'])
    else:
        indicator = _SECTOR_RANK_INDICATORS[period]
        df = _safe_df_call(
            ak.stock_sector_fund_flow_rank, indicator=indicator,
            sector_type='行业资金流', source_name=f'em_sector_rank_industry_{period}',
        )
        data = _parse_sector_rank_table(df)
    _cache_set(cache_key, data)
    return data


def _sector_payload(
    items, board, period='day', q='', sort='net', order='desc', page=1, page_size=50,
    message='',
):
    if q:
        needle = q.lower()
        items = [item for item in items if needle in item['name'].lower() or needle in (item.get('leader') or '').lower()]

    sorted_items = _sort_items(items, _SECTOR_SORT_FIELDS[sort], order)
    page_items, pagination = _paginate(sorted_items, page, page_size)
    inflow = sorted((item for item in items if item['net'] > 0), key=lambda item: item['net'], reverse=True)
    outflow = sorted((item for item in items if item['net'] < 0), key=lambda item: item['net'])
    neutral = [item for item in items if item['net'] == 0]
    positive_total = sum(item['net'] for item in inflow)
    top_three = sum(item['net'] for item in inflow[:3])
    total_net = sum(item['net'] for item in items)
    breadth = len(inflow) / len(items) * 100 if items else None
    divergences = [
        item for item in items
        if item.get('change_pct') is not None
        and ((item['net'] > 0 and item['change_pct'] < 0) or (item['net'] < 0 and item['change_pct'] > 0))
    ]
    divergences.sort(key=lambda item: abs(item['net']), reverse=True)

    return {
        'available': bool(items),
        'board': board,
        'period': period,
        'supported_periods': list(_SECTOR_PERIODS),
        'unavailable_periods': ['1m', '3m', '6m', '1y'],
        'unavailable_reason': '更长区间需要日度快照积累后开放',
        **({'message': message} if message else {}),
        'summary': {
            'sample_count': len(items),
            'net_total': total_net if items else None,
            'inflow_count': len(inflow),
            'outflow_count': len(outflow),
            'neutral_count': len(neutral),
            'breadth_pct': breadth,
            'top_three_inflow_concentration_pct': (top_three / positive_total * 100) if positive_total else None,
            'strongest_inflow': inflow[0] if inflow else None,
            'strongest_outflow': outflow[0] if outflow else None,
        },
        'inflow_top': inflow[:8],
        'outflow_top': outflow[:8],
        'divergences': divergences[:6],
        'items': page_items,
        'pagination': pagination,
        'methodology': '板块资金为上游当日聚合强弱指标，不代表资金在板块之间的真实转移路径。',
    }


def get_sector_rotation(board='industry', period='day', q='', sort='net', order='desc', page=1, page_size=50):
    if board not in ('industry', 'concept'):
        raise BadRequest('board 必须是 industry 或 concept')
    if period not in _SECTOR_PERIODS:
        raise BadRequest('period 必须是 day、5d、10d 或 20d')
    if sort not in _SECTOR_SORT_FIELDS:
        raise BadRequest('不支持的板块排序字段')
    if order not in ('asc', 'desc'):
        raise BadRequest('order 必须是 asc 或 desc')

    if period in _SNAPSHOT_PERIOD_DAYS:
        # 多日轮动只由日度快照供数（DAT-15）；快照只含净额，其他排序字段无值可排
        if sort != 'net':
            raise BadRequest(f'{period} 轮动由日度快照供数，仅支持按净额排序')
        return _snapshot_rotation(
            board, period, q=q, order=order, page=page, page_size=page_size,
        )

    if board == 'industry':
        loader = fetch_industry_fund_flow
        cache_key = f'industry_ff_{period}'
    else:
        loader = fetch_concept_fund_flow
        cache_key = f'concept_ff_{period}'
    payload = _sector_payload(
        loader(period=period), board, period=period,
        q=q, sort=sort, order=order, page=page, page_size=page_size,
    )
    return _rotation_envelope(
        payload,
        _cache_meta(
            cache_key,
            180,
            _THS_DAY_SOURCE[board],
            payload['available'],
            disclaimer=payload['methodology'],
        ),
        updated_at=_now_str(),
        basis='ths_day',
    )


def _rotation_envelope(payload, meta, **head):
    """板块轮动响应：{**head, meta, **payload}，经 envelope 输出（键顺序与旧实现一致，ARC-04）。"""
    rest = {k: v for k, v in payload.items() if k not in ('items', 'pagination')}
    return envelope(
        payload['items'], payload['pagination'], meta,
        key_order=(*head, 'meta', *payload), **head, **rest,
    )


def _snapshot_rotation(board, period, q='', order='desc', page=1, page_size=50):
    """5/10/20 日轮动：由日度快照的当日净额累加得到，窗口不齐则如实不可用。"""
    from . import snapshots
    from ..models import MarketDailySnapshot

    kind = (MarketDailySnapshot.KIND_INDUSTRY_FF if board == 'industry'
            else MarketDailySnapshot.KIND_CONCEPT_FF)
    n = _SNAPSHOT_PERIOD_DAYS[period]
    nets, covered = snapshots.sector_multiday_nets(kind, n)
    try:
        latest = snapshots._latest_snapshot(kind)
    except Exception:
        latest = None

    if nets is None:
        items, message = [], (
            f'日度快照积累不足：已覆盖 {covered}/{n} 个交易日（每天收盘后自动积累，'
            f'缺天不补，凑齐后自动开放）。'
        )
    else:
        items = [
            {'name': name, 'net': net, 'inflow': None, 'outflow': None,
             'change_pct': None, 'index_value': None, 'company_count': None,
             'leader': None, 'leader_pct': None}
            for name, net in nets.items()
        ]
        message = ''
    payload = _sector_payload(
        items, board, period=period, q=q, sort='net', order=order,
        page=page, page_size=page_size, message=message,
    )
    return _rotation_envelope(
        payload,
        snapshots.snapshot_meta(
            f'MarketDailySnapshot({kind}) n={n}（同花顺当日净额逐日累加）',
            bool(items),
            latest=latest,
            message=message,
        ),
        updated_at=_now_str(),
        basis='ths_snapshot',
    )
