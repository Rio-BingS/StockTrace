"""ETF 持有人结构（基金定报，半年频）。

十大持有人明细无结构化数据源（定期报告 PDF 披露），此接口取基金档案的
持有人结构表（机构/个人/内部占比 + 总份额），作为汇金等长线配置盘的
间接观察口径——机构占比含全部机构，不是汇金单独口径，页面须如实标注。
"""

from __future__ import annotations

import logging
import re
from io import StringIO

import pandas as pd
import requests

from ._cache import _cache_get, _cache_meta, _cache_set, _stale_or, _to_float

logger = logging.getLogger(__name__)

_HOLDER_TTL = 7 * 86400  # 半年报/年报披露，长缓存即可
_URL = 'https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=cyrjg&code={code}'
_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/90.0.4430.85 Safari/537.36',
    'Referer': 'https://fundf10.eastmoney.com/',
}


def fetch_holder_structure(code, ttl=_HOLDER_TTL, force=False):
    """单只基金（ETF）的定报持有人结构，items 按公告日期倒序。"""
    code = code.zfill(6)
    key = f'fund_holder_{code}'
    if not force:
        cached = _cache_get(key, ttl)
        if cached is not None:
            return cached

    fail = {
        'available': False,
        'items': [],
        'message': '持有人结构暂不可用（上游异常或无披露）',
        'meta': _cache_meta(key, ttl, '东财基金档案·持有人结构', False),
    }
    try:
        r = requests.get(_URL.format(code=code), headers=_HEADERS, timeout=10)
        r.raise_for_status()
    except Exception as e:
        logger.warning(f'持有人结构拉取失败（{code}）: {e}')
        return _stale_or(key, fail)

    m = re.search(r'content:"(.*)"\};', r.text, re.S)
    if not m:
        return _stale_or(key, fail)
    try:
        tables = pd.read_html(StringIO(m.group(1)))
    except Exception as e:
        logger.warning(f'持有人结构解析失败（{code}）: {e}')
        return _stale_or(key, fail)
    if not tables or tables[0].empty:
        return _stale_or(key, fail)

    items = []
    for _, row in tables[0].iterrows():
        total_yi = _to_float(row.get('总份额（亿份）'))
        items.append({
            'announce_date': str(row.get('公告日期', ''))[:10],
            'institution_pct': _to_float(str(row.get('机构持有比例', '')).strip('%')),
            'individual_pct': _to_float(str(row.get('个人持有比例', '')).strip('%')),
            'internal_pct': _to_float(str(row.get('内部持有比例', '')).strip('%')),
            # 上游单位亿份，统一换算为「份」，与 ETF 现货/快照的 share 同口径（DAT-20）
            'total_shares': total_yi * 1e8 if total_yi is not None else None,
            'total_shares_unit': '份',
        })
    items = [i for i in items if i['announce_date'] and i['announce_date'] != 'nan']
    if not items:
        return _stale_or(key, fail)

    data = {
        'available': True,
        'code': code,
        'items': items[:8],
        'message': '',
        'meta': _cache_meta(
            key, ttl, '东财基金档案·持有人结构（基金定报）', True,
            source_data_date=items[0]['announce_date'],
            disclaimer='定报半年/年频披露、滞后 1-2 个季度；机构占比含汇金/险资/理财等'
                       '全部机构，仅作长线配置盘的间接观察，非汇金单独口径。',
        ),
    }
    _cache_set(key, data)
    return data
