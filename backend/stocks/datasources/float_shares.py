"""流通股本历史（ALG-13）：新浪 StockService.getAmountBySymbol。

返回按变动日期排列的流通 A 股（接口单位万股，这里换算成股）。akshare
stock_zh_a_daily 计算 outstanding_share/turnover 用的就是这个接口；
2026-10-11 抽查 600519、000001、300750、688981、920021，最新一条 ×1e4
与腾讯行情 qt.gtimg.cn 第 72 字段（流通股本，股）逐股一致。

只给探测器换手率维度用：每只自选股每次扫描最多一次请求，结果进程内缓存
6 小时；连续失败 3 次后整体熔断 10 分钟，避免上游故障时每只股票都等超时。
拿不到数据一律返回 None，由调用方如实标注「股本数据缺失」，不抛异常。
上游对没有股本数据的代码（ETF、退市股等）返回 `(null)`：这是正常的「无数据」，
按 6 小时缓存，不计入熔断（否则自选里有几只 ETF 就会把整轮扫描熔断掉）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from bisect import bisect_right
from datetime import date

import requests

from ..common.codes import exchange_prefix

logger = logging.getLogger(__name__)

SINA_SHARE_URL = (
    'https://stock.finance.sina.com.cn/stock/api/jsonp.php/'
    'var%20KKE_ShareAmount_{symbol}=/StockService.getAmountBySymbol'
)
_TIMEOUT = 8
_TTL = 6 * 3600
# 单只股票取数失败后的负缓存秒数（不影响其他股票）
_NEG_TTL = 600
# 连续失败 N 次后整体熔断（秒）
_BREAK_AFTER = 3
_BREAK_SECONDS = 600

_cache = {}  # code -> (写入时刻, 有效秒数, history | None)
_fail_state = {'count': 0, 'until': 0.0}
_lock = threading.Lock()


def enabled():
    """settings 开关：生产恒为 True；测试 runner 关掉，避免隐式触网。"""
    from django.conf import settings

    return getattr(settings, 'STOCKTRACE_FLOAT_SHARES_ENABLED', True)


def parse_share_history(text):
    """解析 jsonp 文本 → [(date, 股数)]，按日期升序；同一日期多条取后出现的一条。

    上游明确返回 `(null)`（该代码没有股本数据）时返回空列表；
    其余结构不对（没有数组、字段缺失、股本非正数）抛 ValueError。
    """
    if text.rstrip().rstrip(';').endswith('(null)'):
        return []
    start, end = text.find('['), text.rfind(']')
    if start < 0 or end < start:
        raise ValueError('流通股本响应里没有数据数组')
    rows = json.loads(text[start:end + 1])
    by_day = {}
    for row in rows:
        day = date.fromisoformat(str(row['date'])[:10])
        shares = float(row['amount']) * 1e4
        if not shares > 0:
            raise ValueError(f'流通股本非正数: {row!r}')
        by_day[day] = shares
    return sorted(by_day.items())


def _download(code):
    symbol = exchange_prefix(code)
    resp = requests.get(
        SINA_SHARE_URL.format(symbol=symbol),
        params={'_': '20', 'symbol': symbol},
        headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn/'},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return parse_share_history(resp.text)


def fetch_float_share_history(code):
    """某只股票的流通股本变动历史；关闭、熔断或取数失败时返回 None。"""
    if not enabled():
        return None
    now = time.time()
    with _lock:
        hit = _cache.get(code)
        if hit is not None:
            ts, ttl, data = hit
            if now - ts < ttl:
                return data
        if now < _fail_state['until']:
            return None
    try:
        data = _download(code)
    except Exception as e:  # noqa: BLE001  任何失败都降级为「股本数据缺失」
        logger.warning(f'流通股本获取失败 {code}: {e}')
        with _lock:
            _cache[code] = (now, _NEG_TTL, None)
            _fail_state['count'] += 1
            if _fail_state['count'] >= _BREAK_AFTER:
                _fail_state['until'] = now + _BREAK_SECONDS
                _fail_state['count'] = 0
                logger.warning(f'流通股本接口连续失败 {_BREAK_AFTER} 次，暂停 {_BREAK_SECONDS}s')
        return None
    if not data:
        logger.info(f'流通股本：{code} 上游无数据（ETF/退市等），按股本数据缺失处理')
    with _lock:
        _cache[code] = (now, _TTL, data or None)
        _fail_state['count'] = 0
    return data or None


def shares_on_dates(history, dates):
    """把变动历史展开成与 dates（ISO 字符串）对齐的逐日流通股本；早于首条记录的日期为 None。"""
    if not history:
        return [None] * len(dates)
    days = [d for d, _ in history]
    values = [v for _, v in history]
    out = []
    for raw in dates:
        idx = bisect_right(days, date.fromisoformat(str(raw)[:10])) - 1
        out.append(values[idx] if idx >= 0 else None)
    return out
