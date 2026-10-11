"""ETF：全表现货、份额雷达、单只详情与国家队观察名单。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import akshare as ak
from django.utils import timezone

from ..common.envelope import envelope
from ..errors import BadRequest
from ._cache import _cache_get, _cache_meta, _cache_set, _now_str, _stale_or, _to_float
from .holders import fetch_holder_structure
from ._query import _paginate, _sort_items
from ._sources import _first_ok, _safe_df_call
from . import snapshots

logger = logging.getLogger(__name__)

# 国家队 / 汇金 常相关的宽基与政策向 ETF（6 位代码）
NATIONAL_TEAM_ETFS = [
    ('510050', '上证50ETF'),
    ('510300', '沪深300ETF'),
    ('159919', '沪深300ETF嘉实'),
    ('510500', '中证500ETF'),
    ('159915', '创业板ETF'),
    ('512100', '中证1000ETF'),
    ('588000', '科创50ETF'),
    ('510180', '上证180ETF'),
    ('159901', '深证100ETF'),
    ('512000', '券商ETF'),
    ('512880', '证券ETF'),
    ('512010', '医药ETF'),
    ('159995', '芯片ETF'),
    ('515790', '光伏ETF'),
    ('512480', '半导体ETF'),
    ('512690', '酒ETF'),
    ('518880', '黄金ETF'),
    ('511010', '国债ETF'),
]

_ETF_SORT_FIELDS = {
    'share': 'share',
    'market_cap': 'market_cap',
    'turnover': 'turnover',
    'main_net': 'main_net',
    'change_pct': 'change_pct',
    'turnover_rate': 'turnover_rate',
    'discount_rate': 'discount_rate',
}
_ETF_RANK_SORT = {
    'share': ('share', 'desc'),
    'market_cap': ('market_cap', 'desc'),
    'turnover': ('turnover', 'desc'),
    'main_inflow': ('main_net', 'desc'),
    'main_outflow': ('main_net', 'asc'),
    'gainer': ('change_pct', 'desc'),
    'loser': ('change_pct', 'asc'),
}
_ETF_SCOPE_EXCLUDE_RULES = (
    ('货币/现金管理 ETF', ('货币ETF', '货币基金', '保证金', '场内货币', '理财金')),
    ('债券 ETF', ('国债ETF', '政金债', '信用债', '城投债', '可转债', '转债ETF', '债券ETF', '地方债', '公司债')),
    ('商品 ETF', ('黄金ETF', '黄金9999', '白银ETF', '原油ETF', '豆粕ETF', '商品ETF', '大宗商品ETF')),
    ('境外市场 ETF', ('纳指', '纳斯达克', '标普', '日经', '德国ETF', '法国ETF', '沙特ETF', '东南亚', '中概互联', '中概互联网')),
    ('香港市场 ETF', ('恒生', '港股', '港股通', '恒科', '香港证券')),
)
_ETF_SCOPE_RULE_VERSION = 'equity-broad-v2'


def _fetch_etf_spot_df(ttl=180, force=False):
    """全市场 ETF 现货（较重，单独缓存）。"""
    if not force:
        cached = _cache_get('etf_spot_df', ttl)
        if cached is not None:
            return cached

    df = _safe_df_call(ak.fund_etf_spot_em, source_name='em_etf_spot')
    if df is None or df.empty:
        return _stale_or('etf_spot_df', None)

    _cache_set('etf_spot_df', df)
    return df


def _etf_scope(item):
    """
    研究用股票/宽基范围：上游暂无稳定基金类型字段，因此采用可审计名称规则。
    排除词要求足够具体，避免误伤自由现金流、黄金股、有色金属等股票 ETF。
    """
    name = item['name'].upper()
    for category, keywords in _ETF_SCOPE_EXCLUDE_RULES:
        matched = next((keyword for keyword in keywords if keyword.upper() in name), None)
        if matched:
            return False, f'{category}:{matched}'
    if 'ETF' in name or '交易型开放式指数' in name:
        return True, '股票/宽基候选:ETF名称规则'
    return False, '无法判定:未命中ETF名称规则'


def _etf_row_to_item(row):
    code = str(row.get('代码', '')).zfill(6)
    item = {
        'code': code,
        'name': str(row.get('名称', '')).strip(),
        'exchange': 'SH' if code.startswith('5') else ('SZ' if code.startswith('1') else None),
        'price': _to_float(row.get('最新价')),
        'iopv': _to_float(row.get('IOPV实时估值')),
        'discount_rate': _to_float(row.get('基金折价率')),
        'change': _to_float(row.get('涨跌额')),
        'change_pct': _to_float(row.get('涨跌幅')),
        'volume': _to_float(row.get('成交量')),
        'turnover': _to_float(row.get('成交额')),
        'prev_close': _to_float(row.get('昨收')),
        'open': _to_float(row.get('今开')),
        'high': _to_float(row.get('最高')),
        'low': _to_float(row.get('最低')),
        'amplitude': _to_float(row.get('振幅')),
        'volume_ratio': _to_float(row.get('量比')),
        'turnover_rate': _to_float(row.get('换手率')),
        'main_net': _to_float(row.get('主力净流入-净额')),
        'main_net_pct': _to_float(row.get('主力净流入-净占比')),
        'super_large_net': _to_float(row.get('超大单净流入-净额')),
        'large_net': _to_float(row.get('大单净流入-净额')),
        'medium_net': _to_float(row.get('中单净流入-净额')),
        'small_net': _to_float(row.get('小单净流入-净额')),
        'share': _to_float(row.get('最新份额')),
        'float_market_cap': _to_float(row.get('流通市值')),
        'market_cap': _to_float(row.get('总市值')),
        'data_date': str(row.get('数据日期', ''))[:10],
        'source_updated_at': str(row.get('更新时间', '')),
    }
    scope_match, reason = _etf_scope(item)
    item['scope_match'] = scope_match
    item['scope_match_reason'] = reason
    return item


def _normalized_etf_items(ttl=180):
    df = _fetch_etf_spot_df(ttl=ttl)
    if df is None or df.empty:
        return []
    items = []
    seen = set()
    for _, row in df.iterrows():
        item = _etf_row_to_item(row)
        if not item['code'] or item['code'] in seen:
            continue
        seen.add(item['code'])
        items.append(item)
    return items


def get_etf_share_radar(
    scope='equity_broad', rank='share', sort=None, order=None, q='',
    min_turnover=None, page=1, page_size=50, ttl=180, force=False,
):
    if scope not in ('equity_broad', 'all'):
        raise BadRequest('scope 必须是 equity_broad 或 all')
    if rank not in _ETF_RANK_SORT:
        raise BadRequest('不支持的 ETF 排行维度')
    if sort is not None and sort not in _ETF_SORT_FIELDS:
        raise BadRequest('不支持的 ETF 排序字段')
    if order is not None and order not in ('asc', 'desc'):
        raise BadRequest('order 必须是 asc 或 desc')

    if force:
        _fetch_etf_spot_df(ttl=ttl, force=True)
    all_items = _normalized_etf_items(ttl=ttl)
    scoped_items = [item for item in all_items if item['scope_match']] if scope == 'equity_broad' else list(all_items)
    if q:
        needle = q.lower()
        scoped_items = [item for item in scoped_items if needle in item['code'].lower() or needle in item['name'].lower()]
    if min_turnover is not None:
        scoped_items = [item for item in scoped_items if item.get('turnover') is not None and item['turnover'] >= min_turnover]

    default_sort, default_order = _ETF_RANK_SORT[rank]
    sort_field = sort or default_sort
    sort_order = order or default_order
    sorted_items = _sort_items(scoped_items, _ETF_SORT_FIELDS[sort_field], sort_order)
    page_items, pagination = _paginate(sorted_items, page, page_size)

    # 份额 n 日变化：由日度快照计算，窗口不齐的档位如实为 null
    share_chg_maps = {n: snapshots.share_change_map(n)[0] for n in (1, 5, 20)}
    for item in page_items:
        for n in (1, 5, 20):
            chg_map = share_chg_maps[n]
            item[f'share_chg_{n}d'] = chg_map.get(item['code']) if chg_map else None

    total_main = sum(item['main_net'] for item in scoped_items if item.get('main_net') is not None)
    total_turnover = sum(item['turnover'] for item in scoped_items if item.get('turnover') is not None)
    data_dates = [item['data_date'] for item in scoped_items if item.get('data_date')]

    return envelope(
        page_items,
        pagination,
        _cache_meta(
            'etf_spot_df',
            ttl,
            'akshare.fund_etf_spot_em',
            bool(all_items),
            source_data_date=max(data_dates) if data_dates else None,
            disclaimer='最新份额与资金流为市场源当前快照；股票/宽基范围为研究用规则筛选，非基金官方分类。',
        ),
        key_order=_ETF_RADAR_KEY_ORDER,
        updated_at=_now_str(),
        available=bool(all_items),
        scope={
            'active': scope,
            'rule_version': _ETF_SCOPE_RULE_VERSION,
            'all_count': len(all_items),
            'equity_broad_count': sum(1 for item in all_items if item['scope_match']),
        },
        summary={
            'count': len(scoped_items),
            'share_available_count': sum(1 for item in scoped_items if item.get('share') is not None),
            'positive_main_net_count': sum(1 for item in scoped_items if (item.get('main_net') or 0) > 0),
            'negative_main_net_count': sum(1 for item in scoped_items if (item.get('main_net') or 0) < 0),
            'total_main_net': total_main,
            'total_turnover': total_turnover,
        },
        rank=rank,
        sort=sort_field,
        order=sort_order,
        supported_metrics={
            'share_change_1d': share_chg_maps[1] is not None,
            'share_change_5d': share_chg_maps[5] is not None,
            'share_change_20d': share_chg_maps[20] is not None,
            'reason': (
                '份额变化由收盘后日度快照计算；档位窗口内缺任一交易日即暂为 null，'
                '快照积累齐全后自动开放。'
            ),
        },
        message='' if all_items else 'ETF 行情暂不可用',
    )


# ETF 雷达已上线的 JSON 键顺序（envelope 按此输出，保证响应逐字节不变，ARC-04）
_ETF_RADAR_KEY_ORDER = (
    'updated_at', 'available', 'meta', 'scope', 'summary', 'rank', 'sort', 'order',
    'pagination', 'items', 'supported_metrics', 'message',
)


def _etf_sina_symbol(code):
    """新浪 ETF 历史接口需要带交易所前缀：5 开头沪市、1 开头深市。"""
    return ('sh' if str(code).startswith('5') else 'sz') + str(code)


def _parse_sina_etf_hist(df):
    """新浪 ETF 日线（date/open/high/low/close/volume，英文列）→ 统一 item。

    新浪不提供成交额/涨跌幅/换手率，如实为 None，不推算凑数。
    """
    items = []
    for _, row in df.iterrows():
        items.append({
            'date': str(row.get('date', ''))[:10],
            'open': _to_float(row.get('open')),
            'close': _to_float(row.get('close')),
            'high': _to_float(row.get('high')),
            'low': _to_float(row.get('low')),
            'volume': _to_float(row.get('volume')),
            'turnover': None,
            'change_pct': None,
            'turnover_rate': None,
        })
    return items


# 各历史源的复权口径（DAT-14）：东财请求前复权；新浪 ETF 日线接口无复权参数
_ETF_HIST_ADJUST = {'em_etf_hist': 'qfq', 'sina_etf_hist': 'none'}
_ADJUST_LABELS = {'qfq': '前复权', 'none': '不复权'}


def _history_cache_key(code, range_name, start_date=None, end_date=None):
    """自定义区间带起止日期入 key；与 _etf_history 内部保持一致（meta 来源标注依赖）。"""
    if start_date:
        return f'etf_history_{code}_custom_{start_date}_{end_date or "today"}'
    return f'etf_history_{code}_{range_name}'


def _etf_history(code, range_name='3m', start_date=None, end_date=None, ttl=1200):
    """range_name 为固定档位；start_date 给定时按自定义起止日期取数（end_date 缺省今天）。"""
    cache_key = _history_cache_key(code, range_name, start_date, end_date)
    cached = _cache_get(cache_key, ttl)
    if cached is not None:
        return cached['items']

    if start_date:
        start = start_date
        end = end_date or timezone.localdate()
    else:
        end = timezone.localdate()
        start = end - timedelta(days=_RANGE_LOOKBACK_DAYS[range_name])
    # 多源路由：东财优先；东财失败/限流时切新浪（新浪无成交额/换手率，字段如实为 None）
    # 东财取前复权（DAT-14）：ETF 份额折算/分红会让不复权价格出现断崖，区间收益失真；
    # 新浪接口不提供复权参数，只能不复权，复权口径见 _ETF_HIST_ADJUST 并在 meta 标注
    df, source = _first_ok([
        (lambda: ak.fund_etf_hist_em(
            symbol=code, period='daily',
            start_date=start.strftime('%Y%m%d'), end_date=end.strftime('%Y%m%d'),
            adjust='qfq',
        ), 'em_etf_hist'),
        (lambda: ak.fund_etf_hist_sina(symbol=_etf_sina_symbol(code)), 'sina_etf_hist'),
    ])
    if df is None or df.empty:
        return (_stale_or(cache_key, None) or {}).get('items', [])

    if source == 'em_etf_hist':
        items = []
        for _, row in df.iterrows():
            items.append({
                'date': str(row.get('日期', ''))[:10],
                'open': _to_float(row.get('开盘')),
                'close': _to_float(row.get('收盘')),
                'high': _to_float(row.get('最高')),
                'low': _to_float(row.get('最低')),
                'volume': _to_float(row.get('成交量')),
                'turnover': _to_float(row.get('成交额')),
                'change_pct': _to_float(row.get('涨跌幅')),
                'turnover_rate': _to_float(row.get('换手率')),
            })
    else:
        items = _parse_sina_etf_hist(df)
        # 新浪返回全量历史，按 [start, end] 双边截断（东财由 start_date/end_date
        # 服务端过滤）；只截起点会让自定义区间混入 end 之后的行（DAT-14）
        start_s, end_s = start.isoformat(), end.isoformat()
        items = [item for item in items if start_s <= item['date'] <= end_s]

    # items 与来源存同一个缓存值（DAT-19）：分两个 key 会被 LRU 分别淘汰，meta 来源误标
    _cache_set(cache_key, {'available': bool(items), 'items': items, 'source': source})
    return items


def _history_source(cache_key):
    """缓存里该段历史的实际来源；无缓存时按主源东财。"""
    return (_stale_or(cache_key, None) or {}).get('source') or 'em_etf_hist'


def _period_return(history, bars):
    closes = [item['close'] for item in history if item.get('close') is not None]
    if len(closes) <= bars or not closes[-bars - 1]:
        return None
    return round((closes[-1] / closes[-bars - 1] - 1) * 100, 3)


_CUSTOM_MAX_SPAN_DAYS = 1100  # 自定义区间上限约 3 年，限制返回体规模（1H2G 红线）

# 自然日回看长度：略大于名义周期（节假日缓冲），避免「半年」显示出 8 个月
_RANGE_LOOKBACK_DAYS = {'1w': 12, '1m': 35, '3m': 100, '6m': 190, '1y': 375}


def _parse_range_date(value, label):
    try:
        return datetime.strptime(str(value), '%Y-%m-%d').date()
    except ValueError:
        raise BadRequest(f'{label} 必须是 YYYY-MM-DD 格式日期')


def _window_stats(history):
    """所选区间的价格统计：区间涨跌以首根收盘为基准，缺字段如实为 None。"""
    closes = [i['close'] for i in history if i.get('close') is not None]
    if not closes:
        return None
    highs = [i['high'] for i in history if i.get('high') is not None]
    lows = [i['low'] for i in history if i.get('low') is not None]
    turnovers = [i['turnover'] for i in history if i.get('turnover') is not None]
    return {
        'change_pct': round((closes[-1] / closes[0] - 1) * 100, 3) if closes[0] else None,
        'high': max(highs) if highs else None,
        'low': min(lows) if lows else None,
        'avg_turnover': round(sum(turnovers) / len(turnovers), 2) if turnovers else None,
        'count': len(history),
    }


def get_etf_detail(code, range_name='3m', start_date=None, end_date=None):
    if not (str(code).isdigit() and len(str(code)) == 6):
        raise BadRequest('ETF 代码必须是 6 位数字')
    if range_name not in ('1w', '1m', '3m', '6m', '1y', 'custom'):
        raise BadRequest('range 必须是 1w、1m、3m、6m、1y 或 custom')
    start = end = None
    if range_name == 'custom':
        if not start_date:
            raise BadRequest('自定义区间必须提供 start_date')
        start = _parse_range_date(start_date, 'start_date')
        end = _parse_range_date(end_date, 'end_date') if end_date else None
        if end is None:
            end = timezone.localdate()
        if start > end:
            raise BadRequest('start_date 不能晚于 end_date')
        if (end - start).days > _CUSTOM_MAX_SPAN_DAYS:
            raise BadRequest(f'自定义区间最长 {_CUSTOM_MAX_SPAN_DAYS} 天（约 3 年）')
    else:
        end = timezone.localdate()
        start = end - timedelta(days=_RANGE_LOOKBACK_DAYS[range_name])

    quote = next((item for item in _normalized_etf_items() if item['code'] == code), None)
    if quote is None:
        raise BadRequest('未在 ETF 行情中找到该代码')
    chg_maps = {n: snapshots.share_change_map(n)[0] for n in (1, 5, 20)}
    has_chg = any(m is not None for m in chg_maps.values())
    if has_chg:
        chg_payload = {f'share_chg_{n}d': (m or {}).get(code) for n, m in chg_maps.items()}
        share_message = '份额变化由日度快照计算；窗口未凑齐的档位为 null。'
    else:
        chg_payload = {f'share_chg_{n}d': None for n in (1, 5, 20)}
        share_message = '尚未积累日度份额快照，暂不提供 1/5/20 日份额变化。'
    history = _etf_history(code, range_name=range_name, start_date=start, end_date=end)
    history_key = _history_cache_key(code, range_name, start, end)
    history_dates = [item['date'] for item in history if item.get('date')]
    history_src = _history_source(history_key)
    history_adjust = _ETF_HIST_ADJUST.get(history_src, 'none')
    share_series = snapshots.share_history_series(code, start, end)
    if share_series:
        share_note = None
    elif quote.get('exchange') == 'SZ':
        share_note = '深交所暂无公开历史份额来源，份额曲线将随本站快照积累逐步可用。'
    else:
        share_note = '所选区间暂无份额快照（沪市历史可在 VPS 用 backfill_etf_share_sse 回填）。'
    return {
        'meta': _cache_meta(
            'etf_spot_df',
            180,
            'akshare.fund_etf_spot_em',
            bool(quote),
            source_data_date=quote.get('data_date'),
            disclaimer='当前行情为 ETF 市场快照；价格历史为日线行情，份额历史仅沪市可回溯（深市随快照积累）。',
        ),
        'instrument': {'code': quote['code'], 'name': quote['name'], 'exchange': quote['exchange']},
        'quote': quote,
        'price_performance': {
            'return_5d': _period_return(history, 5),
            'return_20d': _period_return(history, 20),
            'return_60d': _period_return(history, 60),
        },
        'share_metrics': {
            'latest_share': quote.get('share'),
            **chg_payload,
            'availability': 'daily_snapshot' if has_chg else 'latest_only',
            'message': share_message,
        },
        'share_history': {
            'available': bool(share_series),
            'items': share_series or [],
            'message': share_note,
            # 直接读库、不走进程内缓存：原先套一个从不写入的缓存 key，
            # cache_status 永远是 unavailable（DAT-19）
            'meta': {
                'available': bool(share_series),
                'source': '本站日度快照（沪市历史来自上交所官方接口回填）',
                'source_data_date': share_series[-1]['date'] if share_series else None,
                'data_as_of': share_series[-1]['date'] if share_series else None,
                'fetched_at': None,
                'cache_status': 'fresh' if share_series else 'unavailable',
                'disclaimer': '份额为收盘口径；沪市历史由上交所官方接口回填，深市随本站快照积累。',
            },
        },
        'holder_structure': fetch_holder_structure(code),
        'history': {
            'range': range_name,
            'interval': '1d',
            'available': bool(history),
            'count': len(history),
            'start_date': min(history_dates) if history_dates else None,
            'end_date': max(history_dates) if history_dates else None,
            'stats': _window_stats(history),
            'adjust': history_adjust,
            'meta': _cache_meta(
                history_key,
                1200,
                f'akshare.{history_src}',
                bool(history),
                source_data_date=max(history_dates) if history_dates else None,
                disclaimer=(
                    f'仅为 ETF 市场价格日线（{_ADJUST_LABELS[history_adjust]}）；不包含历史份额。'
                    '东财源为前复权，新浪备源不复权且无成交额/换手率，如实为空。'
                ),
            ),
            'items': history,
        },
    }


_SHARE_SIGNAL_MIN_PCT = 1.0   # 单只份额单日变化阈值（%）：滤掉日常小额申赎噪音
_SHARE_SIGNAL_MIN_ABS = 1e8   # 单只绝对份额变化阈值（1 亿份）：与百分比双条件共同过滤
_SHARE_SIGNAL_SYNC_COUNT = 3  # 同向异动达到该只数才算「同步」（国家队行为的典型特征是多只宽基同日同向）
_SHARE_SIGNAL_TOP = 10


def _share_change_signals():
    """宽基 ETF 单日份额异动信号（启发式，收盘快照口径）。

    观察逻辑：国家队经 ETF 渠道进出场的公开可观察特征是「多只宽基 ETF 同日
    大额净申购/净赎回」。阈值规则只为滤噪音，判定结果不是官方持仓数据。
    """
    try:
        from . import snapshots
        changes, meta = snapshots.latest_pair_change()
    except Exception:
        logger.warning("读取份额快照对比失败，份额异动信号按不可用处理", exc_info=True)
        changes, meta = None, None
    if changes is None:
        return {
            'available': False,
            'message': '份额快照不足两日或已过期，暂无法计算当日份额异动（快照每个交易日收盘后积累）。',
        }

    in_rows, out_rows = [], []
    for code, info in changes.items():
        name = info.get('name') or ''
        in_scope, _why = _etf_scope({'name': name})
        if not in_scope:
            continue
        pct, chg = info.get('chg_pct'), info.get('chg') or 0
        if pct is None or abs(pct) < _SHARE_SIGNAL_MIN_PCT or abs(chg) < _SHARE_SIGNAL_MIN_ABS:
            continue
        share, market_cap = info.get('share'), info.get('market_cap')
        # 净申购金额估算 = 份额变化 × 净值（净值 = 总市值/份额，来自最新快照行；缺市值如实 None）
        nav = market_cap / share if (share and market_cap is not None) else None
        row = {
            'code': code, 'name': name,
            'share_chg': chg, 'share_chg_pct': pct,
            'est_amount': round(chg * nav, 2) if nav is not None else None,
            'market_cap': market_cap, 'turnover': info.get('turnover'),
        }
        (in_rows if chg > 0 else out_rows).append(row)

    in_rows.sort(key=lambda r: abs(r['share_chg']), reverse=True)
    out_rows.sort(key=lambda r: abs(r['share_chg']), reverse=True)
    sync_in = len(in_rows) >= _SHARE_SIGNAL_SYNC_COUNT
    sync_out = len(out_rows) >= _SHARE_SIGNAL_SYNC_COUNT
    if sync_in and sync_out:
        signal = 'mixed'
    elif sync_in:
        signal = 'sync_in'
    elif sync_out:
        signal = 'sync_out'
    else:
        signal = 'none'

    return {
        'available': True,
        'date': meta['date'],
        'prev_date': meta['prev_date'],
        'signal': signal,
        'in_count': len(in_rows),
        'out_count': len(out_rows),
        'in_total_chg': round(sum(r['share_chg'] for r in in_rows), 2) if in_rows else None,
        'out_total_chg': round(sum(r['share_chg'] for r in out_rows), 2) if out_rows else None,
        'in_items': in_rows[:_SHARE_SIGNAL_TOP],
        'out_items': out_rows[:_SHARE_SIGNAL_TOP],
        'thresholds': {
            'pct': _SHARE_SIGNAL_MIN_PCT,
            'abs_share': _SHARE_SIGNAL_MIN_ABS,
            'sync_count': _SHARE_SIGNAL_SYNC_COUNT,
        },
        'message': (
            f'启发式规则：宽基 ETF 单日份额变化≥{_SHARE_SIGNAL_MIN_PCT:g}%且≥{_SHARE_SIGNAL_MIN_ABS/1e8:g}亿份才计入，'
            f'≥{_SHARE_SIGNAL_SYNC_COUNT}只同向视为同步异动；份额变化亦可能来自套利赎回或机构配置，'
            '非官方持仓数据，需结合成交额与季报披露综合判断。'
        ),
    }


def get_national_team_etfs(ttl=180, force=False):
    """国家队相关 ETF 观察名单，不代表官方持仓披露。"""
    cache_key = 'national_etf'
    if force:
        _fetch_etf_spot_df(ttl=ttl, force=True)
    elif (cached := _cache_get(cache_key, ttl)) is not None:
        return cached

    by_code = {item['code']: item for item in _normalized_etf_items(ttl=ttl)}
    items = []
    for code, fallback_name in NATIONAL_TEAM_ETFS:
        item = dict(by_code.get(code) or {
            'code': code,
            'name': fallback_name,
            'price': None,
            'change_pct': None,
            'share': None,
            'main_net': None,
        })
        item['listed'] = code in by_code
        if not item.get('name'):
            item['name'] = fallback_name
        items.append(item)

    listed = [item for item in items if item['listed']]
    total_main = sum(item['main_net'] for item in listed if item.get('main_net') is not None)
    total_share = sum(item['share'] for item in listed if item.get('share') is not None)
    total_market_cap = sum(item['market_cap'] for item in listed if item.get('market_cap') is not None)
    data_dates = [item['data_date'] for item in listed if item.get('data_date')]
    data = {
        'updated_at': _now_str(),
        'available': bool(listed),
        'mode': 'watchlist',
        'official_disclosure_available': False,
        'watchlist_definition': {
            'maintained_by': 'StockTrace curated list',
            'basis': '市场常用宽基与政策相关 ETF 的研究观察清单',
            'is_official_holding': False,
        },
        'meta': _cache_meta(
            'etf_spot_df',
            ttl,
            'akshare.fund_etf_spot_em + curated watchlist',
            bool(listed),
            source_data_date=max(data_dates) if data_dates else None,
            disclaimer='该页面是宽基/政策相关 ETF 观察名单，不是国家队官方持仓披露。',
        ),
        'disclaimer': '该页面是宽基/政策相关 ETF 观察名单，不是国家队官方持仓披露，不提供持仓主体、持仓比例或持仓成本。',
        'summary': {
            'count': len(listed),
            'total_main_net': total_main if listed else None,
            'total_share': total_share if listed else None,
            'total_market_cap': total_market_cap if listed else None,
        },
        'items': items,
        'signals': _share_change_signals(),
        'message': '' if listed else 'ETF 行情暂不可用',
    }
    _cache_set(cache_key, data)
    return data
