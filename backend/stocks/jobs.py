"""行情拉取编排任务：单股完整拉取、全部关注股批量拉取与调度用的定时任务（ARC-01）。

从 services 拆出。每只股票串行、失败互相隔离；交易日判断统一经
trading_calendar（测试打桩点 stocks.trading_calendar.is_trading_day /
is_trading_time）。
"""

import logging
import time
from datetime import timedelta

from django.utils import timezone

from . import trading_calendar
from .ingest import fetch_daily_data, fetch_minute_data, fetch_stock_info
from .models import Stock

logger = logging.getLogger(__name__)


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
        except Exception as e:
            # 名称补全失败不影响数据拉取，但要留痕便于排查
            logger.warning(f"补全 {stock.code} 名称失败（不影响行情拉取）: {e}")

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
        if trading_calendar.is_trading_day(date) is False:
            continue
        date_str = date.strftime('%Y-%m-%d')
        fetch_minute_data(stock, trade_date=date_str)
        fetched += 1
        time.sleep(0.5)

    return daily_count


def _fetch_active_stocks(light, error_fmt):
    """逐只拉取全部活跃关注股（fetch_stock_all_data），单只失败只记日志不中断。

    error_fmt：单只失败时的日志模板（含 {code} / {e}），保持两个入口原有日志文案。
    """
    results = []
    for stock in Stock.objects.filter(is_active=True):
        try:
            count = fetch_stock_all_data(stock, light=light)
            results.append({'code': stock.code, 'status': 'success', 'count': count})
        except Exception as e:
            logger.error(error_fmt.format(code=stock.code, e=e))
            results.append({'code': stock.code, 'status': 'error', 'message': str(e)})
        time.sleep(1)
    return results


def fetch_all_active_stocks(light=True):
    """拉取所有活跃关注股票的数据。默认 light，避免手动全量一次打爆接口。"""
    return _fetch_active_stocks(light, '拉取 {code} 数据失败: {e}')


def fetch_keep_fresh(light=True):
    """
    定时轻量保活：名称 + 增量日线 + 近 1~2 个交易日分钟。
    无历史的新股会 bootstrap 日线（约一年）+ 少量分钟，仍按股串行。

    与 fetch_all_active_stocks 同一实现（A-28），只是日志文案不同并多记一条汇总。
    """
    results = _fetch_active_stocks(light, '保活拉取 {code} 失败: {e}')
    logger.info(f"保活拉取完成: {len(results)} 只")
    return results


def fetch_intraday_update():
    """盘中定时更新：拉取当日分钟数据"""
    if not trading_calendar.is_trading_time():
        logger.debug("非交易时段，跳过盘中更新")
        return

    # 三态：日历未知（None）时写库任务保守跳过
    if not trading_calendar.is_trading_day():
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
    if not trading_calendar.is_trading_day():
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
