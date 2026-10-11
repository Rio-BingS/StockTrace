"""
多数据源股票数据获取与处理服务

数据源优先级（自动切换）：
1. 东方财富 (em) - AkShare stock_zh_a_hist / stock_zh_a_hist_min_em
2. 新浪 (sina) - AkShare stock_zh_a_daily / stock_zh_a_minute
3. BaoStock (bs) - BaoStock query_history_k_data_plus (5分钟K线)
4. 腾讯 (tx) - AkShare stock_zh_a_hist_tx

核心逻辑：
1. 获取日K线数据 → open, close, high, low, volume, turnover
2. 获取分钟K线数据 → 推算最高/最低点的精确时间
3. 计算差值和百分比
4. 存入数据库

入库单位约定（DAT-03）：成交量统一为「股」，成交额统一为「元」，由各数据源适配器
在返回前换算（东财 手→股 ×100；新浪/BaoStock 原生为股；腾讯日线只有成交量(手)、
无成交额）。DailyQuote/MinuteBar.source 记录实际来源，MinuteBar.freq 记录 bar 周期。
"""

import logging
import time

import akshare as ak
import pandas as pd

from . import trading_calendar
from .common.codes import exchange, exchange_prefix
# 数据源适配器与多源 failover 已拆到 datasources 包（ARC-01），旧名在此重导出
from .datasources import (  # noqa: F401
    DATA_SOURCES,
    NAME_SOURCES,
    BaoStockSource,
    DataSource,
    EastMoneySource,
    SinaSource,
    TencentSource,
    _filter_minute_range,
    _source_call,
    _stock_backoff,
    _to_numeric_cols,
    _try_sources,
    _volume_lots_to_shares,
)
# BaoStock 会话串行化与交易日历已分别抽到 baostock_session / trading_calendar（ARC-02），
# 旧名保留供既有调用方使用
from .baostock_session import bs_lock as _bs_lock, bs_session as _bs_session  # noqa: F401
# 日 K / 分钟 bar 入库已拆到 ingest（ARC-01），旧名在此重导出
from .ingest import (  # noqa: F401
    _ADJ_ABS_TOL,
    _ADJ_REL_TOL,
    _AMOUNT_Q,
    _DAILY_FINAL_TIME,
    _DAILY_UPDATE_FIELDS,
    _MINUTE_UPDATE_FIELDS,
    _MINUTE_VALUE_FIELDS,
    _PRICE_Q,
    _adjust_basis_changed,
    _daily_start_date,
    _day_bounds,
    _dec2,
    _dec3,
    _full_history_start,
    _is_final_bar,
    _save_minute_bars,
    compute_high_low_times,
    fetch_daily_data,
    fetch_minute_data,
    fetch_stock_info,
    format_symbol,
)
# 拉取编排与定时任务已拆到 jobs（ARC-01），旧名在此重导出
from .jobs import (  # noqa: F401
    fetch_all_active_stocks,
    fetch_daily_summary,
    fetch_incremental_daily_all,
    fetch_intraday_update,
    fetch_keep_fresh,
    fetch_stock_all_data,
)

logger = logging.getLogger(__name__)

# A股列表缓存（30分钟过期）
_stock_list_cache = {'df': None, 'expires': 0}
_STOCK_LIST_TTL = 1800

# ============================================================
# 工具函数
# ============================================================

# 交易所判定与前缀格式已抽到 common.codes（ARC-01 / A-28），旧私有名保留一版
_exchange = exchange


def _to_shsz_prefix(code):
    """6位代码转为交易所前缀格式: 600000→sh600000, 000001→sz000001, 830799→bj830799"""
    return exchange_prefix(code)


# ============================================================
# 核心业务逻辑
# ============================================================

def is_trading_time(dt=None):
    """判断当前是否在A股交易时段内（薄包装，实现见 trading_calendar，ARC-02）"""
    return trading_calendar.is_trading_time(dt)


def _reset_trade_calendar_cache():
    """清空交易日历全部缓存（薄包装，实现见 trading_calendar，ARC-02）"""
    trading_calendar.reset_cache()


def is_trading_day(date=None):
    """三态交易日判断 True / False / None（薄包装，实现见 trading_calendar，ARC-02）"""
    return trading_calendar.is_trading_day(date)


# ============================================================
# 股票搜索
# ============================================================

def _get_stock_list():
    """获取A股全量列表（带缓存）"""
    now = time.time()
    if _stock_list_cache['df'] is not None and now < _stock_list_cache['expires']:
        return _stock_list_cache['df']
    try:
        df = ak.stock_info_a_code_name()
        _stock_list_cache['df'] = df
        _stock_list_cache['expires'] = now + _STOCK_LIST_TTL
        logger.info(f"A股列表缓存刷新，共 {len(df)} 条")
        return df
    except Exception as e:
        logger.error(f"获取A股列表失败: {e}")
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
