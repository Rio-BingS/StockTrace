"""A 股交易日历：全项目唯一实现（ARC-02）。

此前日历逻辑散落在 services（三态 is_trading_day）、market/snapshots（近 N 个
交易日、最近已完成交易日）、market/sentiment（候选日、前一交易日）与
market/_cache（最近收盘时刻）四处，market 包还要反向 import services。现在
所有模块只依赖本模块；本模块只依赖上游数据源（新浪整表 + BaoStock 单日），
不依赖任何业务模块。旧位置保留同名薄包装转调，行为不变。

测试打桩：统一 patch ``stocks.trading_calendar.is_trading_day``——本模块内的
派生函数与 services.is_trading_day 包装都经它查询，一处生效。

交易日历缓存（DAT-07）：
- 整表（新浪）带 TTL，过期后刷新失败继续沿用旧表（日历极少变化），只在表覆盖
  范围内给出确定结论；范围外不再一律判为非交易日
- 单日结论（BaoStock 兜底）按 date 缓存且带 TTL：date -> (bool, expires_at)
- 上游失败做负缓存（冷却期内不再请求），避免日历故障时逐日重复打接口
- 全部不可用时返回 None（未知），不再把「仅排除周末」的猜测结果永久缓存
"""

import logging
import time
from datetime import date as _date, datetime, time as dt_time, timedelta

import akshare as ak
import pandas as pd
from django.utils import timezone

from .baostock_session import bs_session

logger = logging.getLogger(__name__)

_trade_day_cache = {}
_trade_calendar_set = None
_trade_calendar_range = None   # (min_date, max_date)
_trade_calendar_expires = 0
_TRADE_CAL_TTL = 6 * 3600
_TRADE_DAY_TTL = 6 * 3600
_CALENDAR_FAIL_TTL = 300
_calendar_fail_until = {'sina': 0, 'baostock': 0}

# 往回找交易日的自然日上限（覆盖春节/国庆等长假，也是研究窗口上限）
MAX_LOOKBACK_DAYS = 120
# 最近收盘时刻记忆（60 秒）与回看天数
_session_close_memo = {'close': None, 'at': 0.0}
_SESSION_CLOSE_LOOKBACK_DAYS = 20

# 「已完成交易日」的时点：15:30 后才算当天（覆盖快照写入时点）
_SESSION_DONE_TIME = dt_time(15, 30)


def reset_cache():
    """清空交易日历全部缓存（测试 / 运维用）"""
    global _trade_calendar_set, _trade_calendar_range, _trade_calendar_expires
    _trade_day_cache.clear()
    _trade_calendar_set = None
    _trade_calendar_range = None
    _trade_calendar_expires = 0
    for k in _calendar_fail_until:
        _calendar_fail_until[k] = 0
    _session_close_memo.update({'close': None, 'at': 0.0})


def _refresh_trade_calendar(now):
    """按需刷新新浪整表交易日历；失败进入冷却，保留旧表"""
    global _trade_calendar_set, _trade_calendar_range, _trade_calendar_expires
    if _trade_calendar_set is not None and now < _trade_calendar_expires:
        return
    if now < _calendar_fail_until['sina']:
        return
    try:
        trade_dates = ak.tool_trade_date_hist_sina()
        days = set(pd.to_datetime(trade_dates['trade_date']).dt.date)
        if not days:
            raise ValueError('交易日历为空')
        _trade_calendar_set = days
        _trade_calendar_range = (min(days), max(days))
        _trade_calendar_expires = now + _TRADE_CAL_TTL
    except Exception as e:
        _calendar_fail_until['sina'] = now + _CALENDAR_FAIL_TTL
        logger.warning(
            f"新浪交易日历获取失败（{_CALENDAR_FAIL_TTL}s 内不再重试）"
            f"{'，沿用旧表' if _trade_calendar_set is not None else ''}: {e}"
        )


def _baostock_is_trading_day(date, now):
    """BaoStock 单日查询；返回 True/False，失败或无结果返回 None（失败进入冷却）"""
    if now < _calendar_fail_until['baostock']:
        return None
    date_str = date.strftime('%Y-%m-%d')
    try:
        with bs_session() as bs:
            rs = bs.query_trade_dates(start_date=date_str, end_date=date_str)
            err = getattr(rs, 'error_code', '0')
            if isinstance(err, str) and err != '0':
                raise RuntimeError(f"查询失败: {err} {getattr(rs, 'error_msg', '')}")
            while rs.next():
                row = rs.get_row_data()
                return row[1] == '1'
    except Exception as e:
        _calendar_fail_until['baostock'] = now + _CALENDAR_FAIL_TTL
        logger.warning(f"BaoStock交易日历获取失败（{_CALENDAR_FAIL_TTL}s 内不再重试）: {e}")
    return None


def is_trading_day(date=None):
    """判断是否为交易日（排除周末和节假日），三态：True / False / None（未知）

    多源备用 + 带 TTL 的缓存。日历全部不可用、或日期超出日历覆盖范围且兜底源
    也查不到时返回 None：写库类调用方（`if not is_trading_day()`）会保守跳过，
    只读类调用方可用 `is False` 只排除明确的非交易日。
    """
    if date is None:
        # 本地交易日（timezone.now() 是 UTC，凌晨会落到前一天）
        date = timezone.localdate()

    if date.weekday() >= 5:
        return False

    now = time.time()

    # 方式1: 新浪交易日历（AkShare）整表，只在表覆盖范围内下结论
    _refresh_trade_calendar(now)
    if _trade_calendar_set is not None and _trade_calendar_range[0] <= date <= _trade_calendar_range[1]:
        return date in _trade_calendar_set

    # 方式2: BaoStock 单日查询（结论带 TTL 缓存）
    cached = _trade_day_cache.get(date)
    if cached is not None and now < cached[1]:
        return cached[0]
    result = _baostock_is_trading_day(date, now)
    if result is not None:
        _trade_day_cache[date] = (result, now + _TRADE_DAY_TTL)
        return result

    # 兜底: 未知（不缓存结论；上游失败已有冷却，不会逐日重复请求）
    logger.debug(f"交易日历无法判定 {date}，返回未知")
    return None


def is_trading_time(dt=None):
    """判断当前是否在A股交易时段内（只看钟点，不查日历）"""
    if dt is None:
        dt = timezone.now()

    local_time = dt.astimezone(timezone.get_default_timezone())
    hour, minute = local_time.hour, local_time.minute

    # 上午 9:30 - 11:30
    if hour == 9 and minute >= 30:
        return True
    if hour == 10:
        return True
    if hour == 11 and minute <= 30:
        return True

    # 下午 13:00 - 15:00
    if hour == 13:
        return True
    if hour == 14:
        return True
    if hour == 15 and minute == 0:
        return True

    return False


def recent_trading_days(n, end_date=None):
    """截止 end_date（含）往前数 n 个已完成交易日；不足时返回 None（含日历不可用）。"""
    end_date = end_date or timezone.localdate()
    days = []
    day = end_date
    for _ in range(MAX_LOOKBACK_DAYS):
        if is_trading_day(day):
            days.append(day)
            if len(days) == n:
                days.reverse()
                return days
        day -= timedelta(days=1)
    return None


def expected_latest_date():
    """最近一个已完成交易日的日期（15:30 后才算当天，覆盖快照写入时点）。

    日历不可用时返回 None（调用方按「无法校验时效」处理）。
    """
    now = timezone.localtime()
    day = now.date()
    for _ in range(MAX_LOOKBACK_DAYS):
        if day < now.date() or now.time() >= _SESSION_DONE_TIME:
            try:
                if is_trading_day(day):
                    return day
            except Exception:
                return None
        day -= timedelta(days=1)
    return None


def recent_day_candidates(n=4, today=None):
    """按「今天优先、其后最近交易日」生成候选日（探测上游披露日用）。

    is_trading_day 仅在明确 False（周末/节假日）时跳过；日历不可用时的
    节假日候选会多探测一次空结果，属可接受代价。
    """
    # 沿用原 sentiment 实现的 date.today()（进程本地时区），行为不变
    d = today or _date.today()
    if is_trading_day(d) is False:
        d -= timedelta(days=1)
    out = []
    while len(out) < n:
        if is_trading_day(d) is not False:
            out.append(d)
        d -= timedelta(days=1)
    return out


def prev_trading_day(d):
    """d 之前最近一个「不是明确非交易日」的日期（日历未知时按交易日处理）"""
    prev = d - timedelta(days=1)
    while is_trading_day(prev) is False:
        prev -= timedelta(days=1)
    return prev


def last_session_close():
    """最近已完成交易日的 15:00（本地时区、aware datetime）；日历不可用时返回 None。

    结果记忆 60 秒；最多回看 20 天以覆盖春节/国庆等长假。
    """
    now = timezone.localtime()
    if now.timestamp() - _session_close_memo['at'] < 60:
        return _session_close_memo['close']
    close = None
    day = now.date()
    try:
        for _ in range(_SESSION_CLOSE_LOOKBACK_DAYS):
            if is_trading_day(day):
                candidate = timezone.make_aware(datetime.combine(day, dt_time(15, 0)))
                if candidate <= now:
                    close = candidate
                    break
            day -= timedelta(days=1)
    except Exception:
        close = None
    _session_close_memo['close'] = close
    _session_close_memo['at'] = now.timestamp()
    return close


def data_changing_now(now):
    """交易日 09:15–21:00 数据仍可能变化（盘中 + 收盘后晚到）。"""
    if not is_trading_day(now.date()):
        return False
    return dt_time(9, 15) <= now.time() <= dt_time(21, 0)
