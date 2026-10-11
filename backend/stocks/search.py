"""股票代码/名称搜索（A 股全量列表带 30 分钟进程内缓存，从 services 拆出，ARC-01）。"""

import logging
import time

import akshare as ak
import pandas as pd

logger = logging.getLogger(__name__)

# A股列表缓存（30分钟过期）
_stock_list_cache = {'df': None, 'expires': 0}
_STOCK_LIST_TTL = 1800


def _get_stock_list():
    """获取A股全量列表（带缓存）"""
    now = time.time()
    if _stock_list_cache['df'] is not None and now < _stock_list_cache['expires']:
        return _stock_list_cache['df']
    try:
        df = ak.stock_info_a_code_name()
        _stock_list_cache['df'] = df
        _stock_list_cache['expires'] = now + _STOCK_LIST_TTL
        logger.info("A股列表缓存刷新，共 %s 条", len(df))
        return df
    except Exception as e:
        # 上游边界：akshare 异常类型不固定；有旧缓存用旧缓存，否则返回空表降级
        logger.warning("获取A股列表失败: %s", e, exc_info=True)
        if _stock_list_cache['df'] is not None:
            return _stock_list_cache['df']
        return pd.DataFrame(columns=['code', 'name'])


def search_stocks(keyword, limit=20):
    """
    按代码或名称模糊搜索股票

    Args:
        keyword: 搜索关键词
        limit: 最多返回条数

    Returns:
        list[dict]: [{'code': '000001', 'name': '平安银行'}, ...]
    """
    if not keyword or len(keyword.strip()) < 2:
        return []

    df = _get_stock_list()
    if df.empty:
        return []

    kw = keyword.strip()
    # regex=False：关键词是用户输入，按字面匹配，避免非法/恶意正则 500 或回溯
    mask = df['name'].str.contains(kw, case=False, na=False, regex=False) | \
           df['code'].str.contains(kw, case=False, na=False, regex=False)
    matches = df[mask].head(limit)
    return matches.to_dict('records')
