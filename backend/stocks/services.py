"""services 兼容外观（ARC-01）：原「多数据源股票数据获取与处理服务」已按职责拆分。

- datasources/：四个上游适配器（东财/新浪/BaoStock/腾讯）、数据源优先级与多源
  failover（_try_sources）；
- ingest：日 K / 分钟 bar 入库、复权基准检测、高低点时间推算；
- jobs：单股完整拉取、批量拉取与调度定时任务；
- search：A 股代码/名称搜索；
- trading_calendar / baostock_session / common.codes：交易日历、BaoStock 会话与
  证券代码工具。

本模块只做重导出，保证 `stocks.services.X` 旧导入路径可用一版；新代码请直接从
上述模块导入。注意打桩：外观里的名字只是引用，patch 'stocks.services.X' 不影响新
模块内部调用，测试须 patch 实际定义/查找该名字的模块（见 ARC2_NOTES_services）。
"""

from . import trading_calendar
from .baostock_session import bs_lock as _bs_lock, bs_session as _bs_session
from .common.codes import exchange, exchange_prefix
from .datasources import (
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
from .ingest import (
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
from .jobs import (
    fetch_all_active_stocks,
    fetch_daily_summary,
    fetch_incremental_daily_all,
    fetch_intraday_update,
    fetch_keep_fresh,
    fetch_stock_all_data,
)
from .search import _STOCK_LIST_TTL, _get_stock_list, _stock_list_cache, search_stocks

# 交易所判定旧私有名（ARC-01 / A-28，实现见 common.codes）
_exchange = exchange


def _to_shsz_prefix(code):
    """6位代码转为交易所前缀格式: 600000→sh600000, 000001→sz000001, 830799→bj830799"""
    return exchange_prefix(code)


def is_trading_time(dt=None):
    """判断当前是否在A股交易时段内（薄包装，实现见 trading_calendar，ARC-02）"""
    return trading_calendar.is_trading_time(dt)


def _reset_trade_calendar_cache():
    """清空交易日历全部缓存（薄包装，实现见 trading_calendar，ARC-02）"""
    trading_calendar.reset_cache()


def is_trading_day(date=None):
    """三态交易日判断 True / False / None（薄包装，实现见 trading_calendar，ARC-02）"""
    return trading_calendar.is_trading_day(date)


__all__ = [
    # 数据源（datasources）
    'DataSource', 'EastMoneySource', 'SinaSource', 'TencentSource', 'BaoStockSource',
    'DATA_SOURCES', 'NAME_SOURCES', '_try_sources', '_source_call', '_stock_backoff',
    '_filter_minute_range', '_to_numeric_cols', '_volume_lots_to_shares',
    # 入库（ingest）
    'fetch_stock_info', 'format_symbol', 'fetch_daily_data', 'fetch_minute_data',
    'compute_high_low_times', '_save_minute_bars', '_adjust_basis_changed',
    '_daily_start_date', '_full_history_start', '_is_final_bar', '_day_bounds',
    '_dec2', '_dec3', '_DAILY_UPDATE_FIELDS', '_DAILY_FINAL_TIME', '_ADJ_REL_TOL',
    '_ADJ_ABS_TOL', '_PRICE_Q', '_AMOUNT_Q', '_MINUTE_VALUE_FIELDS', '_MINUTE_UPDATE_FIELDS',
    # 编排与定时任务（jobs）
    'fetch_stock_all_data', 'fetch_all_active_stocks', 'fetch_keep_fresh',
    'fetch_intraday_update', 'fetch_daily_summary', 'fetch_incremental_daily_all',
    # 搜索（search）
    'search_stocks', '_get_stock_list', '_stock_list_cache', '_STOCK_LIST_TTL',
    # 交易日历与代码工具（薄包装 / 旧名）
    'is_trading_day', 'is_trading_time', '_reset_trade_calendar_cache',
    '_exchange', '_to_shsz_prefix', '_bs_lock', '_bs_session',
]
