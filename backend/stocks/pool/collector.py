"""成分池限速日线采集：单线程队列，全天慢慢拉，绝不并发。

为什么是队列而不是「收盘后一口气拉 800 只」：VPS 是 1H2G，上游对境外 IP
本就脆弱，突发请求会触发限流、拖慢自选股抓取。用户口径是「不需要一次性扫完，
全天运行都没有问题」，所以每 POOL_FETCH_INTERVAL_SECONDS 秒（默认 20）最多
处理一只，一次一个上游请求，顺序执行。

每次 tick 只做一件事，优先级：
1. 增量：已回补的个股，库内最新已收盘日线早于「期望交易日」（15:30 后为当天，
   之前为上一交易日）——收盘后为每只补当天这根 K 线；
2. 回补：已收盘日线不足 POOL_MIN_BARS 根且没回补过——拉 POOL_BACKFILL_CALENDAR_DAYS
   自然日（约 270 个交易日）；本地已够根数的直接标记回补完成，不再请求；
3. 股本：流通股本缓存超过 7 天——刷新一次（扫描时成分池只读缓存，不触网）。

失败处理：
- 单只失败：该股指数退避（5 分钟起翻倍，上限 6 小时），不挡队列里的其他股票；
- 连续 3 次失败（上游整体故障）：整个队列暂停，60 秒起翻倍、上限 30 分钟；
- 数据源层沿用 datasources._try_sources 的 SourceChain 冷却（同源连续失败 2 次冷却 180 秒）。
停牌：拉取成功但仍没有当日 bar 时 1 小时后复查，复查 3 次仍没有就不再为这一天请求。

自选股（Stock.is_active=True）不进本队列：它们走原有 stock_quotes 任务，逻辑不变。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import Count, Max
from django.utils import timezone

logger = logging.getLogger(__name__)

STOCK_BACKOFF_BASE = 300
STOCK_BACKOFF_MAX = 6 * 3600
QUEUE_FAIL_STREAK = 3
QUEUE_BACKOFF_BASE = 60
QUEUE_BACKOFF_MAX = 1800
STALE_RECHECK_SECONDS = 3600
STALE_RECHECK_LIMIT = 3
FLOAT_SHARES_TTL = timedelta(days=7)

MODE_INCREMENT = 'increment'
MODE_BACKFILL = 'backfill'
MODE_FLOAT = 'float_shares'

# 队列级退避状态（进程内：调度进程与回补命令各自一份，二者由 pool_quotes 任务锁互斥）
_queue_state = {'fail_streak': 0, 'pause_until': 0.0}


def interval_seconds():
    return max(5, int(getattr(settings, 'POOL_FETCH_INTERVAL_SECONDS', 20)))


def backfill_days():
    return int(getattr(settings, 'POOL_BACKFILL_CALENDAR_DAYS', 400))


def min_bars():
    return int(getattr(settings, 'POOL_MIN_BARS', 250))


def enabled():
    return bool(getattr(settings, 'POOL_FETCH_ENABLED', True))


def reset_queue_state():
    _queue_state['fail_streak'] = 0
    _queue_state['pause_until'] = 0.0


def queue_paused(now_ts=None):
    """队列整体退避中返回剩余秒数，否则 0。"""
    now_ts = time.time() if now_ts is None else now_ts
    return max(0.0, _queue_state['pause_until'] - now_ts)


def _daily_sources():
    """按 POOL_DAILY_SOURCES 的名字顺序取 datasources.chain.DATA_SOURCES 里的源。"""
    from ..datasources import chain

    by_name = {s.name: s for s in chain.DATA_SOURCES}
    names = getattr(settings, 'POOL_DAILY_SOURCES', None) or [s.name for s in chain.DATA_SOURCES]
    picked = [by_name[n] for n in names if n in by_name]
    return picked or list(chain.DATA_SOURCES)


def pool_codes():
    """需要本队列采集的代码：成分池 − 自选股（自选走原有抓取）。"""
    from ..models import IndexConstituent, Stock

    members = set(IndexConstituent.objects.values_list('code', flat=True))
    watch = set(Stock.objects.filter(is_active=True, code__in=list(members)).values_list('code', flat=True))
    return sorted(members - watch)


def _bar_stats(codes):
    """{code: (已收盘根数, 最新已收盘日)}，一次分组查询。"""
    from ..models import DailyQuote

    out = {}
    rows = (DailyQuote.objects.filter(stock__code__in=list(codes), is_final=True)
            .values('stock__code').annotate(n=Count('id'), last=Max('trade_date')))
    for r in rows:
        out[r['stock__code']] = (r['n'], r['last'])
    return out


def sync_states(codes=None):
    """为成分池里还没有游标的代码建行（根数/最新日从 DailyQuote 回读）；返回新建数。"""
    from ..models import PoolStockState

    codes = pool_codes() if codes is None else codes
    have = set(PoolStockState.objects.filter(code__in=codes).values_list('code', flat=True))
    missing = [c for c in codes if c not in have]
    if not missing:
        return 0
    stats = _bar_stats(missing)
    PoolStockState.objects.bulk_create([
        PoolStockState(code=c, bar_count=stats.get(c, (0, None))[0], last_bar_date=stats.get(c, (0, None))[1])
        for c in missing
    ], ignore_conflicts=True)
    return len(missing)


def next_item(now=None, expected=None, modes=(MODE_INCREMENT, MODE_BACKFILL, MODE_FLOAT)):
    """按优先级挑下一只要处理的股票，返回 (PoolStockState, mode) 或 (None, None)。"""
    from django.db.models import Q

    from ..models import PoolStockState

    now = now or timezone.now()
    codes = pool_codes()
    base = PoolStockState.objects.filter(code__in=codes).filter(
        Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
    if MODE_INCREMENT in modes and expected is not None:
        st = (base.filter(backfilled_at__isnull=False)
              .filter(Q(last_bar_date__isnull=True) | Q(last_bar_date__lt=expected))
              .exclude(checked_date=expected).order_by('last_attempt_at', 'code').first())
        if st is not None:
            return st, MODE_INCREMENT
    if MODE_BACKFILL in modes:
        # 本地已够根数的直接标记回补完成（已有数据就跳过，不请求上游）
        enough = base.filter(backfilled_at__isnull=True, bar_count__gte=min_bars())
        if enough.exists():
            enough.update(backfilled_at=now)
        st = base.filter(backfilled_at__isnull=True).order_by('last_attempt_at', 'code').first()
        if st is not None:
            return st, MODE_BACKFILL
    if MODE_FLOAT in modes:
        st = (base.filter(Q(float_shares_at__isnull=True) | Q(float_shares_at__lt=now - FLOAT_SHARES_TTL))
              .filter(backfilled_at__isnull=False).order_by('float_shares_at', 'code').first())
        if st is not None:
            return st, MODE_FLOAT
    return None, None


def _pool_stock(code):
    from ..models import IndexConstituent, Stock

    stock = Stock.objects.filter(code=code).first()
    if stock is None:
        name = IndexConstituent.objects.filter(code=code).values_list('name', flat=True).first() or code
        stock, _ = Stock.objects.get_or_create(code=code, defaults={'name': name, 'is_active': False})
    return stock


def _refresh_bar_stats(state):
    from ..models import DailyQuote

    agg = DailyQuote.objects.filter(stock__code=state.code, is_final=True).aggregate(
        n=Count('id'), last=Max('trade_date'))
    state.bar_count = agg['n'] or 0
    state.last_bar_date = agg['last']


def _end_date(expected):
    """日线请求的截止日：期望交易日；日历不可用时取昨天（不拉盘中未收盘的当日 bar）。"""
    if expected is not None:
        return expected
    return timezone.localdate() - timedelta(days=1)


def _record_failure(state, now, err):
    state.fail_count = min(state.fail_count + 1, 30)
    delay = min(STOCK_BACKOFF_BASE * 2 ** (state.fail_count - 1), STOCK_BACKOFF_MAX)
    state.next_attempt_at = now + timedelta(seconds=delay)
    state.last_error = str(err)[:200]
    _queue_state['fail_streak'] += 1
    if _queue_state['fail_streak'] >= QUEUE_FAIL_STREAK:
        over = _queue_state['fail_streak'] - QUEUE_FAIL_STREAK
        pause = min(QUEUE_BACKOFF_BASE * 2 ** over, QUEUE_BACKOFF_MAX)
        _queue_state['pause_until'] = time.time() + pause
        logger.warning('成分池采集连续失败 %s 次，队列暂停 %ss', _queue_state['fail_streak'], pause)
    return delay


def _record_success(state):
    state.fail_count = 0
    state.last_error = ''
    state.next_attempt_at = None
    _queue_state['fail_streak'] = 0
    _queue_state['pause_until'] = 0.0


def process(state, mode, expected=None, now=None):
    """处理一只股票（一次上游请求）；返回结果 dict。不 sleep，节奏由调用方控制。"""
    from ..ingest import fetch_daily_data

    now = now or timezone.now()
    state.last_attempt_at = now
    result = {'code': state.code, 'mode': mode}
    if mode == MODE_FLOAT:
        from ..datasources import float_shares

        history = float_shares.fetch_float_share_history(state.code)
        if history:
            state.float_shares = [[d.isoformat(), v] for d, v in history]
            state.float_shares_at = now
            _record_success(state)
            result['status'] = 'ok'
        else:
            # 取不到（上游无数据/失败/熔断）：保留旧缓存，1 天后再试，不在这只上反复请求
            state.float_shares_at = now - FLOAT_SHARES_TTL + timedelta(days=1)
            result['status'] = 'empty'
        state.save()
        return result

    end = _end_date(expected)
    start = None
    if mode == MODE_BACKFILL:
        start = (timezone.localdate() - timedelta(days=backfill_days())).strftime('%Y%m%d')
    try:
        stock = _pool_stock(state.code)
        n = fetch_daily_data(stock, start_date=start, end_date=end.strftime('%Y%m%d'),
                             sources=_daily_sources())
    except Exception as e:  # noqa: BLE001 上游/解析边界：单只失败只退避自己
        logger.warning('成分池日线 %s（%s）失败: %s', state.code, mode, e)
        result['status'] = 'error'
        result['retry_in'] = _record_failure(state, now, e)
        state.save()
        return result
    if not n:
        # 全部源失败或冷却中（_try_sources 已记日志）：按失败退避
        result['status'] = 'error'
        result['retry_in'] = _record_failure(state, now, '所有数据源均未返回日线')
        state.save()
        return result

    _record_success(state)
    _refresh_bar_stats(state)
    if mode == MODE_BACKFILL:
        state.backfilled_at = now
    if expected is not None:
        if state.last_bar_date is not None and state.last_bar_date >= expected:
            state.checked_date = expected
            state.stale_checks = 0
        elif mode == MODE_INCREMENT:
            # 拉取成功但仍没有当日 bar：上游晚到或停牌，1 小时后复查，3 次后放弃这一天
            state.stale_checks += 1
            if state.stale_checks >= STALE_RECHECK_LIMIT:
                state.checked_date = expected
                state.stale_checks = 0
                result['gave_up'] = True
            else:
                state.next_attempt_at = now + timedelta(seconds=STALE_RECHECK_SECONDS)
    state.save()
    result.update(status='ok', rows=n, bars=state.bar_count, last=state.last_bar_date)
    return result


def tick(now=None, modes=(MODE_INCREMENT, MODE_BACKFILL, MODE_FLOAT)):
    """队列走一步：最多处理一只股票。返回结果 dict（idle/paused/disabled 时说明原因）。"""
    from .. import trading_calendar

    if not enabled():
        return {'status': 'disabled'}
    paused = queue_paused()
    if paused:
        return {'status': 'paused', 'remaining': round(paused)}
    sync_states()
    expected = trading_calendar.expected_latest_date()
    state, mode = next_item(now=now, expected=expected, modes=modes)
    if state is None:
        return {'status': 'idle', 'expected': expected}
    return process(state, mode, expected=expected, now=now)


def run_loop(modes=(MODE_BACKFILL,), limit=None, interval=None, sleep=time.sleep,
             clock=time.monotonic, stop=None, on_result=None):
    """回补命令用：按固定间隔连续 tick，直到没有活、达到 limit 或 stop() 为真。

    间隔按「两次请求的开始时刻」计：上一次耗时 4 秒、间隔 20 秒，就再等 16 秒。
    队列整体退避时睡到退避结束再继续。返回处理过的结果列表。
    """
    interval = interval_seconds() if interval is None else max(0.0, float(interval))
    results = []
    while limit is None or len(results) < limit:
        if stop is not None and stop():
            break
        paused = queue_paused()
        if paused:
            sleep(paused)
            continue
        started = clock()
        res = tick(modes=modes)
        if res['status'] in ('idle', 'disabled'):
            break
        results.append(res)
        if on_result is not None:
            on_result(res)
        if limit is not None and len(results) >= limit:
            break
        wait = interval - (clock() - started)
        if wait > 0:
            sleep(wait)
    return results


def coverage(trade_date, codes=None):
    """成分池在 trade_date 的日线完整度：{'expected', 'fresh', 'missing': [代码], 'ratio'}。

    expected 只算已回补过的成分股（新纳入、尚未回补的个股另列 not_ready，不拖垮完整度）。
    """
    from ..models import PoolStockState
    from .constituents import pool_members

    members = sorted(pool_members()) if codes is None else sorted(codes)
    stats = _bar_stats(members)
    states = {s.code: s for s in PoolStockState.objects.filter(code__in=members)}
    ready, missing, not_ready = [], [], []
    for code in members:
        n, last = stats.get(code, (0, None))
        st = states.get(code)
        if last is not None and last >= trade_date:
            ready.append(code)
        elif n >= 30 or (st is not None and st.backfilled_at is not None):
            missing.append(code)
        else:
            not_ready.append(code)
    expected = len(ready) + len(missing)
    return {
        'members': len(members),
        'expected': expected,
        'fresh': len(ready),
        'missing': missing,
        'not_ready': not_ready,
        'ratio': (len(ready) / expected) if expected else 0.0,
    }


def status_summary(now=None):
    """队列概况（命令行 --status 与 DEPLOY 估算用）。"""
    from ..models import PoolStockState
    from .. import trading_calendar

    now = now or timezone.now()
    qs = PoolStockState.objects.filter(code__in=pool_codes())
    total = qs.count()
    backfilled = qs.filter(backfilled_at__isnull=False).count()
    expected = trading_calendar.expected_latest_date()
    fresh = qs.filter(last_bar_date__gte=expected).count() if expected else None
    failing = qs.filter(fail_count__gt=0).count()
    remaining = total - backfilled
    return {
        'pool_codes': total,
        'backfilled': backfilled,
        'backfill_remaining': remaining,
        'expected_date': expected,
        'fresh_for_expected': fresh,
        'failing': failing,
        'interval_seconds': interval_seconds(),
        'backfill_eta_hours': round(remaining * interval_seconds() / 3600, 1),
        'queue_paused_seconds': round(queue_paused()),
        'as_of': datetime.fromtimestamp(now.timestamp()).isoformat(timespec='seconds'),
    }
