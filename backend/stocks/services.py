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
from datetime import timedelta

import akshare as ak
import pandas as pd
from django.utils import timezone

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
from .models import Stock

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


def fetch_stock_all_data(stock, days_back=5, light=False):
    """
    完整数据拉取：日K线 + 分钟K线（推算高低点时间）

    Args:
        stock: Stock 模型实例
        days_back: 往回扫多少自然日找交易日分钟数据（实际最多 5 个交易日）
        light: True 时只补名称 + 增量日线 + 最近 2 个交易日分钟（适合添加后自动拉）
    """
    # 补全股票名称（如果名称还是代码占位或为空）
    if not stock.name.strip() or stock.name == stock.code:
        try:
            real_name = fetch_stock_info(stock.code)
            if real_name and real_name != stock.code:
                stock.name = real_name
                stock.save(update_fields=['name'])
        except Exception:
            pass  # 名称补全失败不影响数据拉取

    daily_count = fetch_daily_data(stock)

    max_trading_days = 2 if light else min(max(days_back, 1), 5)
    today = timezone.localdate()
    fetched = 0
    # 多扫几天自然日以凑够交易日
    for i in range(max_trading_days * 3 + 5):
        if fetched >= max_trading_days:
            break
        date = today - timedelta(days=i)
        # 日历未知（None）时照常尝试：分钟线已按日期过滤，非交易日只会拿到空结果
        if is_trading_day(date) is False:
            continue
        date_str = date.strftime('%Y-%m-%d')
        fetch_minute_data(stock, trade_date=date_str)
        fetched += 1
        time.sleep(0.5)

    return daily_count


def fetch_all_active_stocks(light=True):
    """拉取所有活跃关注股票的数据。默认 light，避免手动全量一次打爆接口。"""
    stocks = Stock.objects.filter(is_active=True)
    results = []
    for stock in stocks:
        try:
            count = fetch_stock_all_data(stock, light=light)
            results.append({'code': stock.code, 'status': 'success', 'count': count})
        except Exception as e:
            logger.error(f"拉取 {stock.code} 数据失败: {e}")
            results.append({'code': stock.code, 'status': 'error', 'message': str(e)})
        time.sleep(1)
    return results


def fetch_intraday_update():
    """盘中定时更新：拉取当日分钟数据"""
    if not is_trading_time():
        logger.debug("非交易时段，跳过盘中更新")
        return

    # 三态：日历未知（None）时写库任务保守跳过
    if not is_trading_day():
        logger.debug("非交易日或交易日历未知，跳过盘中更新")
        return

    stocks = Stock.objects.filter(is_active=True)
    for stock in stocks:
        try:
            fetch_minute_data(stock)
        except Exception as e:
            logger.error(f"盘中更新 {stock.code} 失败: {e}")
        time.sleep(0.8)


def fetch_daily_summary():
    """收盘后汇总：拉取当日完整数据并计算"""
    if not is_trading_day():
        logger.debug("非交易日或交易日历未知，跳过收盘汇总")
        return

    stocks = Stock.objects.filter(is_active=True)
    for stock in stocks:
        try:
            # 本地日期（DAT-18）；带少量重叠，防止昨日缺漏
            today = timezone.localdate()
            today_str = today.strftime('%Y%m%d')
            start = (today - timedelta(days=3)).strftime('%Y%m%d')
            fetch_daily_data(stock, start_date=start, end_date=today_str)
            fetch_minute_data(stock)
        except Exception as e:
            logger.error(f"收盘汇总 {stock.code} 失败: {e}")
        time.sleep(1)


def fetch_incremental_daily_all():
    """
    定时：仅增量日线（有历史从最近 bar 重叠几天；无历史 bootstrap）。
    用于开盘前/晚间预热，避免用户手动时才补大段数据。
    """
    stocks = Stock.objects.filter(is_active=True)
    ok, fail = 0, 0
    for stock in stocks:
        try:
            n = fetch_daily_data(stock)
            ok += 1
            logger.info(f"增量日线 {stock.code}: {n} 条")
        except Exception as e:
            fail += 1
            logger.error(f"增量日线 {stock.code} 失败: {e}")
        time.sleep(0.8)
    logger.info(f"增量日线完成 ok={ok} fail={fail}")
    return {'ok': ok, 'fail': fail}


def fetch_keep_fresh(light=True):
    """
    定时轻量保活：名称 + 增量日线 + 近 1~2 个交易日分钟。
    无历史的新股会 bootstrap 日线（约一年）+ 少量分钟，仍按股串行。
    """
    stocks = Stock.objects.filter(is_active=True)
    results = []
    for stock in stocks:
        try:
            count = fetch_stock_all_data(stock, light=light)
            results.append({'code': stock.code, 'status': 'success', 'count': count})
        except Exception as e:
            logger.error(f"保活拉取 {stock.code} 失败: {e}")
            results.append({'code': stock.code, 'status': 'error', 'message': str(e)})
        time.sleep(1)
    logger.info(f"保活拉取完成: {len(results)} 只")
    return results


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
