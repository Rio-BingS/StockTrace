"""指数成分刷新：沪深300（000300）、中证500（000905）。

数据源顺序：中证指数官网成分文件（akshare index_stock_cons_csindex）→ 新浪成分
（akshare index_stock_cons）。只要有一个源返回「数量合理」的成分就整表替换该指数；
全部失败或数量明显不对（成分文件截断、接口改版）时保留旧值，不清空、不半替换。
"""

from __future__ import annotations

import logging
import re
from datetime import date

from django.db import transaction

logger = logging.getLogger(__name__)

# (指数代码, 名称, 标准成分数)
POOL_INDICES = (
    ('000300', '沪深300', 300),
    ('000905', '中证500', 500),
)
# 数量容差：成分数偏离标准值超过 5% 视为上游异常（截断/改版），不入库
COUNT_TOLERANCE = 0.05
_CODE_RE = re.compile(r'^\d{6}$')


class ConstituentFetchError(RuntimeError):
    """某指数全部数据源都没拿到合理的成分：调用方须保留旧值。"""


def _expected_count(index_code):
    for code, _name, n in POOL_INDICES:
        if code == index_code:
            return n
    return None


def _fetch_csindex(index_code):
    import akshare as ak

    df = ak.index_stock_cons_csindex(symbol=index_code)
    rows = []
    for _, r in df.iterrows():
        as_of = None
        try:
            as_of = date.fromisoformat(str(r.get('日期'))[:10])
        except (TypeError, ValueError):
            as_of = None
        rows.append({'code': str(r.get('成分券代码', '')).zfill(6),
                     'name': str(r.get('成分券名称') or '').strip(), 'as_of': as_of})
    return rows


def _fetch_sina(index_code):
    import akshare as ak

    df = ak.index_stock_cons(symbol=index_code)
    return [{'code': str(r.get('品种代码', '')).zfill(6),
             'name': str(r.get('品种名称') or '').strip(), 'as_of': None}
            for _, r in df.iterrows()]


# (来源标记, 取数函数)；测试 patch 本列表
SOURCES = [
    ('csindex', _fetch_csindex),
    ('sina', _fetch_sina),
]


def validate_rows(index_code, rows):
    """去重 + 校验代码格式与数量；不合格抛 ValueError（说明原因）。"""
    seen = {}
    for row in rows or []:
        code = str(row.get('code') or '')
        if not _CODE_RE.match(code):
            raise ValueError(f'成分券代码格式不对：{code!r}')
        seen[code] = row
    expected = _expected_count(index_code)
    n = len(seen)
    if expected and abs(n - expected) > expected * COUNT_TOLERANCE:
        raise ValueError(f'成分数 {n} 偏离标准 {expected} 超过 {COUNT_TOLERANCE:.0%}')
    if not n:
        raise ValueError('成分为空')
    return list(seen.values())


def fetch_constituents(index_code):
    """按源顺序取某指数成分，返回 (rows, source)；全部失败抛 ConstituentFetchError。"""
    errors = []
    for tag, fn in SOURCES:
        try:
            rows = validate_rows(index_code, fn(index_code))
        except Exception as e:  # noqa: BLE001 上游边界：akshare 异常类型不固定，换下一个源
            logger.warning('指数成分 %s 从 %s 获取失败: %s', index_code, tag, e)
            errors.append(f'{tag}: {e}')
            continue
        return rows, tag
    raise ConstituentFetchError(f'{index_code} 全部成分源失败：' + '；'.join(errors))


def replace_constituents(index_code, rows, source):
    """整表替换某指数成分（单事务）；返回 (新增, 移出) 数。"""
    from ..models import IndexConstituent

    new_codes = {r['code'] for r in rows}
    with transaction.atomic():
        old_codes = set(IndexConstituent.objects.filter(index_code=index_code)
                        .values_list('code', flat=True))
        IndexConstituent.objects.filter(index_code=index_code).exclude(code__in=new_codes).delete()
        for r in rows:
            IndexConstituent.objects.update_or_create(
                index_code=index_code, code=r['code'],
                defaults={'name': r.get('name') or '', 'as_of': r.get('as_of'), 'source': source},
            )
    return len(new_codes - old_codes), len(old_codes - new_codes)


def refresh_constituents(indices=None):
    """刷新成分池；返回 {指数: {'status': 'ok'|'kept', ...}}。

    单个指数失败不影响另一个；失败的指数库里旧成分原样保留（status=kept）。
    成功后为新成分补建 Stock 行（is_active=False，不进自选），供日线入库外键使用。
    """
    from ..models import IndexConstituent

    result = {}
    for index_code, name, _n in POOL_INDICES:
        if indices and index_code not in indices:
            continue
        try:
            rows, source = fetch_constituents(index_code)
        except ConstituentFetchError as e:
            kept = IndexConstituent.objects.filter(index_code=index_code).count()
            logger.warning('指数成分 %s 刷新失败，保留旧值 %s 只: %s', name, kept, e)
            result[index_code] = {'status': 'kept', 'count': kept, 'error': str(e)}
            continue
        added, removed = replace_constituents(index_code, rows, source)
        logger.info('指数成分 %s 刷新完成：%s 只（%s），新增 %s、移出 %s',
                    name, len(rows), source, added, removed)
        result[index_code] = {'status': 'ok', 'count': len(rows), 'source': source,
                              'added': added, 'removed': removed}
    ensure_pool_stocks()
    return result


def pool_members():
    """当前成分池 {代码: 名称}（两个指数并集）。"""
    from ..models import IndexConstituent

    return dict(IndexConstituent.objects.order_by('code').values_list('code', 'name'))


def ensure_pool_stocks():
    """为成分池里还没有 Stock 行的代码建行（is_active=False）。

    DailyQuote 外键指向 Stock；用不在自选（is_active=False）的 Stock 行承载成分池
    日线，自选列表/盘中抓取/提醒都只看 is_active=True，互不干扰。用户把它加进
    自选时，现有的加自选接口会把这行重新激活，已攒的日线直接可用。
    """
    from ..models import Stock

    members = pool_members()
    existing = set(Stock.objects.filter(code__in=list(members)).values_list('code', flat=True))
    missing = [Stock(code=code, name=name or code, is_active=False)
               for code, name in members.items() if code not in existing]
    if missing:
        Stock.objects.bulk_create(missing, ignore_conflicts=True)
    return len(missing)
