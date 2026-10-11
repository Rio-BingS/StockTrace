"""迭代优化：回看 h（=5）个交易日前信号的兑现情况，统计超额并（可选）微调维度参数。

兑现（hit）定义（ALG-05 修订，#12 口径 v2）：
- 窗口：信号日 T 之后的连续交易日（按交易日历校验），后续数据不满或有缺口时
  保持待回看，不判定；
- 基期：指数/个股用 T+1 开盘价（信号 17:15 收盘后才产出，T 日收盘不可成交）；
  板块快照只有日涨跌幅、没有开盘价，改用 T+1 收盘价为基期，再看其后 h 个收盘
  （T+2..T+h+1）——旧口径以 T 收盘为基期，吃进了 T→T+1 的隔夜跳空，板块日
  收益正自相关时偏乐观。两者都是「信号产出后第一个可成交价」：个股/指数是
  T+1 开盘，板块拿不到开盘只能退到 T+1 收盘，所以板块要多等一个交易日才判定；
- 阈值：k × σ20 × √h（σ20 为 T 日及之前 20 个日涨跌幅的标准差）——旧口径 1×σ20
  是「日」尺度，5 日内随机游走约 45% 能触及；σ 不可得时以 HIT_PCT 作 σ；
- 判定：窗口内按收盘价逐日推进，有利方向先达到 +阈值 记兑现；不利方向先
  触及 −阈值（MAE 先到）或到期都没到，记未兑现；
- 另记 h 日收益与同区间沪深300 收益之差（相对基准超额），保留幅度信息；
  基准取不到时超额留空，不影响兑现判定。

统计口径：
- 命中率与同标的「无条件基线」比较：用 T 日之前的历史，以同样的方向、阈值、
  基期、窗口规则统计任意起点的兑现率，看超额命中率而不是绝对值；
- 独立样本：同一交易日、同一方向、同一对象簇（板块一簇、指数一簇、自选股一簇）
  的信号共享同一段市场走势，按簇计为一个样本（簇内取平均）。

自动调参（#5）：
- 总开关 settings.DETECTOR_AUTO_TUNE，默认关闭：关闭时照常统计，显著的结论
  只写演练流水（DetectorTuneLog source=dry_run），不改 DetectorDimConfig；
- 样本跨日累计：取 TUNE_WINDOW_DAYS 内已用 v2 口径判定、有基线的段首信号
  （cycles == 2，同一段的后续日窗口重叠，不计入），按上面的规则聚类；
- 显著才调：Beta(1,1) 先验下 P(命中率 > 平均基线) ≥ TUNE_CONFIDENCE 才上调、
  ≤ 1−TUNE_CONFIDENCE 才下调；独立样本 < MIN_ADJ_SAMPLES 不调；
- 调什么（假设写在这里）：旧实现调 min，但 min 被噪声闸门完全覆盖、改了不影响
  投票，是空操作；改为调真正决定「是否投票」的参数（TUNE_PARAMS，当前为
  noise_pct），以及权重（目前只进入强度 score/分级；若投票汇总改为加权票数，
  权重会直接起作用，参数名不变）。没有可调闸门参数的维度只调权重；
- 怎么调：目标值是「默认值 + 有效超额的函数」，有效超额 = 超额 × n/(n+n0)
  向 0（即默认值）收缩，再夹到上下限。目标值只由窗口内样本决定、不在当前值上
  累乘，同样的证据重复评估不会把参数一路推到边界；
- 留痕：每次生效/演练写一行 DetectorTuneLog（永久保留，#16），生效的同时照旧
  写进 DetectorRun.adjustments 兼容旧读法。
"""

from __future__ import annotations

import logging
import math
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from ..models import DailyQuote, DetectorDimConfig, DetectorSignal, DetectorTuneLog
from ..trading_calendar import MAX_LOOKBACK_DAYS, recent_trading_days
# 阈值数值集中在 constants.py（ARC-06），这里保留同名引用，review.X 旧路径照常可用
from .constants import (
    BASELINE_DAYS,
    BENCHMARK_CODE,
    HIT_PCT,
    HIT_SIGMA_K,
    MIN_ADJ_SAMPLES,
    MIN_BASELINE_WINDOWS,
    NOISE_DEFAULTS,
    REVIEW_LOOKBACK,
    REVIEW_MAX_AGE_DAYS,
    REVIEW_VERSION,
    SEGMENT_HEAD_CYCLES as _SEGMENT_HEAD_CYCLES,
    SIGMA_WINDOW,
    TUNE_CONFIDENCE,
    TUNE_MIN_CHANGE,
    TUNE_PARAMS,
    TUNE_PRIOR_N,
    TUNE_WINDOW_DAYS,
    WEIGHT_BOUNDS,
    WEIGHT_GAIN,
)
from .dimensions import _std, load_sector_snapshots

logger = logging.getLogger(__name__)


def _next_trading_days(after, n, calendar=None):
    """after 之后（不含）的 n 个交易日，升序；日历给不出结论（None/异常）返回 None。

    calendar：可注入的交易日历（带三态 is_trading_day(day) 的对象，离线回测用），
    缺省走 stocks.trading_calendar。
    """
    if calendar is not None:
        is_trading_day = calendar.is_trading_day
    else:
        from ..trading_calendar import is_trading_day

    days, day = [], after
    for _ in range(n * 3 + 15):
        day += timedelta(days=1)
        try:
            flag = is_trading_day(day)
        except Exception:
            logger.warning("复盘的交易日判定失败，按日历未知处理", exc_info=True)
            return None
        if flag is None:
            return None
        if flag:
            days.append(day)
            if len(days) == n:
                return days
    return None


def _sigma(closes):
    """末尾 SIGMA_WINDOW 个日涨跌幅（%）的标准差；数据不齐返回 None。"""
    tail = closes[-(SIGMA_WINDOW + 1):]
    if len(tail) < SIGMA_WINDOW + 1 or any(c is None or c <= 0 for c in tail):
        return None
    rets = [(tail[i] / tail[i - 1] - 1) * 100 for i in range(1, len(tail))]
    return _std(rets)


def _judge(base, path, direction, thr):
    """按收盘价逐日推进：有利先到 +thr 记兑现，不利先到 −thr 或到期未达记未兑现。

    path: [(date, close)]；返回 (hit, hit_date, mfe%)。mfe 为窗口内最大有利波动。
    """
    sign = 1 if direction == 'up' else -1
    mfe = None
    for trade_date, close in path:
        fav = (close / base - 1) * 100 * sign
        mfe = fav if mfe is None else max(mfe, fav)
        if fav <= -thr:
            return False, None, mfe
        if fav >= thr:
            return True, trade_date, mfe
    return False, None, mfe


def _sigma_k():
    """兑现阈值系数 k（settings.DETECTOR_REVIEW_SIGMA_K 可覆盖常量）。"""
    try:
        k = float(getattr(settings, 'DETECTOR_REVIEW_SIGMA_K', HIT_SIGMA_K))
    except (TypeError, ValueError):
        k = HIT_SIGMA_K
    return k if k > 0 else HIT_SIGMA_K


def _threshold(sigma, k=None):
    """h 日兑现阈值（%）：k × σ × √h；σ 不可得时以 HIT_PCT 作日波动。"""
    k = _sigma_k() if k is None else k
    return k * (sigma if sigma else HIT_PCT) * math.sqrt(REVIEW_LOOKBACK)


def _offset(base_is_open):
    """基期相对 T 的偏移：开盘基期用 T+1 开盘（窗口 T+1..T+h），收盘基期用
    T+1 收盘（窗口 T+2..T+h+1），后者多占一个交易日。"""
    return 0 if base_is_open else 1


def _baseline(bars, direction, thr, base_is_open):
    """无条件基线：用信号日之前的历史，任意起点按同规则的兑现率。

    bars: 信号日 T 及之前的 [(date, open, close)] 升序（不含 T 之后，避免前视）。
    起点 s 的基期与窗口按 _offset 与信号同口径，且必须完全落在 T 及之前。
    样本窗口不足返回 None。
    """
    off = _offset(base_is_open)
    n = len(bars)
    last_start = n - 1 - REVIEW_LOOKBACK - off
    first_start = max(0, last_start - BASELINE_DAYS + 1)
    hits = total = 0
    for s in range(first_start, last_start + 1):
        window = bars[s + 1 + off:s + 1 + off + REVIEW_LOOKBACK]
        base = bars[s + 1][1] if base_is_open else bars[s + 1][2]
        if not base or any(c is None for _d, _o, c in window):
            continue
        hit, _d, _m = _judge(base, [(d, c) for d, _o, c in window], direction, thr)
        hits += hit
        total += 1
    if total < MIN_BASELINE_WINDOWS:
        return None
    return hits / total


def _benchmark_bars(cache):
    """基准（沪深300）日线 {date: (open, close)}；取不到返回 {}（超额留空）。"""
    key = ('benchmark', BENCHMARK_CODE)
    if key in cache:
        return cache[key]
    from datetime import date as _date

    from ..market import indices

    bars = {}
    try:
        # days 与 _index_outcome 一致：同一缓存键，scan 预热后事务内不触网
        data = indices.fetch_index_daily_series(BENCHMARK_CODE, days=130)
        if data.get('available'):
            for it in data['items']:
                bars[_date.fromisoformat(it['date'])] = (it.get('open'), it.get('close'))
    except Exception:
        logger.warning("复盘取基准 %s 日线失败，相对基准超额留空", BENCHMARK_CODE, exc_info=True)
    cache[key] = bars
    return bars


def _bench_return(bench, base_date, end_date, base_is_open):
    """基准在同一区间（同一基期口径）的收益（%）；缺数据返回 None。"""
    if not bench:
        return None
    start, end = bench.get(base_date), bench.get(end_date)
    if not start or not end:
        return None
    base = start[0] if base_is_open else start[1]
    if not base or end[1] is None:
        return None
    return (end[1] / base - 1) * 100


def _evaluate_bars(signal, before, after, base_is_open, bench=None, calendar=None):
    """统一判定：before = T 及之前的 bars（末根必须是 T），after = T 之后的 bars。

    base_is_open=True：T+1 开盘为基期，窗口 T+1..T+h；False：T+1 收盘为基期，
    窗口 T+2..T+h+1（板块）。bench 为基准 {date: (open, close)} 或返回它的
    无参函数（窗口凑齐后才调用，待回看的信号不触发基准取数），可缺。
    返回 None（保持待回看）或 dict(hit, hit_date, mfe, thr, baseline,
    fwd_return, bench_return, excess_return)。
    calendar 见 _next_trading_days（缺省不透传，测试 patch 旧签名照常生效）。
    """
    if not before or before[-1][0] != signal.trade_date:
        return None
    off = _offset(base_is_open)
    if calendar is not None:
        expected = _next_trading_days(signal.trade_date, REVIEW_LOOKBACK + off, calendar=calendar)
    else:
        expected = _next_trading_days(signal.trade_date, REVIEW_LOOKBACK + off)
    span = after[:REVIEW_LOOKBACK + off]
    # 后续必须是连续交易日（数据缺口/不足都保持待回看）
    if expected is None or [b[0] for b in span] != expected:
        return None
    if any(c is None for _d, _o, c in span):
        return None
    base = span[0][1] if base_is_open else span[0][2]
    if not base:
        return None
    window = span[off:]
    sigma = _sigma([c for _d, _o, c in before])
    thr = _threshold(sigma)
    hit, hit_date, mfe = _judge(base, [(d, c) for d, _o, c in window], signal.direction, thr)
    fwd = (window[-1][2] / base - 1) * 100
    if callable(bench):
        bench = bench()
    bench_ret = _bench_return(bench, span[0][0], window[-1][0], base_is_open)
    return {
        'hit': hit, 'hit_date': hit_date, 'mfe': mfe, 'thr': thr,
        'baseline': _baseline(before, signal.direction, thr, base_is_open),
        'fwd_return': fwd,
        'bench_return': bench_ret,
        'excess_return': (fwd - bench_ret) if bench_ret is not None else None,
    }


def _stock_outcome(signal, cache):
    from ..models import Stock

    stock = Stock.objects.filter(code=signal.target_code).first()
    if stock is None:
        return None
    # 只用已收盘日线（DAT-13）：盘中当日 bar 的收盘价是盘中价，算出的
    # 前瞻收益会随拉取时刻漂移，不能用来调权重
    qs = DailyQuote.objects.filter(stock=stock, is_final=True)
    need_before = SIGMA_WINDOW + BASELINE_DAYS + REVIEW_LOOKBACK + 1
    before = list(
        qs.filter(trade_date__lte=signal.trade_date)
        .order_by('-trade_date')
        .values_list('trade_date', 'open_price', 'close_price')[:need_before]
    )[::-1]
    after = list(
        qs.filter(trade_date__gt=signal.trade_date)
        .order_by('trade_date')
        .values_list('trade_date', 'open_price', 'close_price')[:REVIEW_LOOKBACK]
    )

    def conv(rows):
        return [(d, float(o) if o is not None else None, float(c) if c is not None else None)
                for d, o, c in rows]

    return _evaluate_bars(signal, conv(before), conv(after), base_is_open=True,
                          bench=lambda: _benchmark_bars(cache))


def _index_outcome(signal, cache):
    from datetime import date as _date

    from ..market import indices

    try:
        data = indices.fetch_index_daily_series(signal.target_code, days=130)
    except Exception:
        logger.warning("复盘取指数 %s 日线失败", signal.target_code, exc_info=True)
        return None
    if not data.get('available'):
        return None
    bars = [(_date.fromisoformat(it['date']), it.get('open'), it.get('close'))
            for it in data['items']]
    before = [b for b in bars if b[0] <= signal.trade_date]
    after = [b for b in bars if b[0] > signal.trade_date]
    return _evaluate_bars(signal, before, after, base_is_open=True,
                          bench=lambda: _benchmark_bars(cache))


def _sector_outcome(signal, cache):
    """板块：快照涨跌幅链式合成伪价格（基期任意），以 T+1 日收盘为基期（无开盘价）。"""
    key = ('sector', signal.target_kind)
    if key not in cache:
        cache[key] = load_sector_snapshots(signal.target_kind, lookback_days=160)
    level, bars = 100.0, []
    for trade_date, by_name in cache[key]:
        row = by_name.get(signal.target_code)
        pct = row.get('change_pct') if row else None
        if pct is None:
            # 缺口：该点不可用，前后水平不可比——缺口前的历史作废，从缺口后重新起算
            if trade_date <= signal.trade_date:
                bars = []
            else:
                bars.append((trade_date, None, None))
            continue
        level *= 1 + pct / 100
        bars.append((trade_date, None, level))
    before = [b for b in bars if b[0] <= signal.trade_date]
    after = [b for b in bars if b[0] > signal.trade_date]
    return _evaluate_bars(signal, before, after, base_is_open=False,
                          bench=lambda: _benchmark_bars(cache))


def _recent_days(n, end_date, calendar=None):
    """截止 end_date（含）的 n 个交易日升序；calendar 为可注入日历，缺省走全局日历。"""
    if calendar is None:
        return recent_trading_days(n, end_date=end_date)
    days, day = [], end_date
    for _ in range(MAX_LOOKBACK_DAYS):
        if calendar.is_trading_day(day):
            days.append(day)
            if len(days) == n:
                return days[::-1]
        day -= timedelta(days=1)
    return None


def review_pending(scan_date, configs, calendar=None):
    """回看、统计并（开关开启时）调参。返回 summary（写进 DetectorRun）。

    只回看 status=active 的信号：watching（周期 1）从未满足「维持」，
    不算一次真正的信号假设，不参与命中率统计，也不调参。
    calendar：可注入交易日历（回看截止日计算用），缺省行为不变。
    """
    days = _recent_days(REVIEW_LOOKBACK + 1, scan_date, calendar)
    if days is None:
        return {'reviewed': 0, 'hits': 0, 'hit_rate': None,
                'note': '交易日历不可用，本轮跳过回看'}
    cutoff = days[0]
    pending = list(
        DetectorSignal.objects.filter(
            status=DetectorSignal.STATUS_ACTIVE,
            review_hit__isnull=True,
            trade_date__lte=cutoff,
            trade_date__gte=scan_date - timedelta(days=REVIEW_MAX_AGE_DAYS),
        ).order_by('trade_date')
    )

    reviewed, hits = 0, 0
    baselines = []
    cache = {}

    for sig in pending:
        if sig.target_kind == 'stock':
            outcome = _stock_outcome(sig, cache)
        elif sig.target_kind == 'index':
            outcome = _index_outcome(sig, cache)
        else:
            outcome = _sector_outcome(sig, cache)
        if outcome is None:
            continue
        hit = outcome['hit']
        sig.review_hit = hit
        sig.hit_date = outcome['hit_date'] if hit else None
        sig.reviewed_at = timezone.now()
        sig.status = (
            DetectorSignal.STATUS_CONFIRMED if hit else DetectorSignal.STATUS_MISSED
        )
        sig.review_date = scan_date
        sig.review_version = REVIEW_VERSION
        sig.review_threshold = _round(outcome['thr'], 4)
        sig.review_baseline = _round(outcome['baseline'], 4)
        sig.fwd_return = _round(outcome['fwd_return'], 4)
        sig.bench_return = _round(outcome['bench_return'], 4)
        sig.excess_return = _round(outcome['excess_return'], 4)
        sig.benchmark_code = BENCHMARK_CODE if outcome['bench_return'] is not None else ''
        sig.save(update_fields=[
            'review_hit', 'hit_date', 'reviewed_at', 'status', 'review_date', 'review_version',
            'review_threshold', 'review_baseline', 'fwd_return', 'bench_return', 'excess_return',
            'benchmark_code', 'updated_at',
        ])
        reviewed += 1
        hits += 1 if hit else 0
        if outcome['baseline'] is not None:
            baselines.append(outcome['baseline'])

    adjustments, tune_stats = [], []
    if reviewed:
        # 有新判定才重新评估（目标值只由窗口样本决定，没有新样本结论不变）
        adjustments, tune_stats = tune_dims(scan_date, configs)

    return {
        'reviewed': reviewed,
        'hits': hits,
        'hit_rate': round(hits / reviewed, 3) if reviewed else None,
        'baseline_rate': round(sum(baselines) / len(baselines), 3) if baselines else None,
        'adjustments': adjustments,
        'tune_stats': tune_stats,
    }


def _round(value, digits):
    return round(value, digits) if value is not None else None


def _cluster_key(sig):
    """独立样本的聚类键：同交易日、同方向、同对象簇的信号共享同一段市场走势。

    对象簇：行业/概念板块一簇（板块间相关很高），指数一簇，自选股一簇
    （Stock 表没有行业字段，无法再细分；偏保守，样本数只会被低估）。
    """
    group = 'sector' if sig.target_kind in ('industry', 'concept') else sig.target_kind
    return (sig.trade_date, sig.direction, group)


def review_stats(review_date):
    """某扫描日做出的 v2 口径判定的汇总：命中/基线/超额命中/独立样本/相对基准超额。

    从库里按 review_date 重算（而不是累加），同日重跑幂等。没有 v2 判定时各项为 None。
    """
    rows = list(
        DetectorSignal.objects.filter(review_date=review_date, review_version__gte=2)
        .only('trade_date', 'direction', 'target_kind', 'review_hit',
              'review_baseline', 'excess_return')
    )
    empty = {'baseline_rate': None, 'excess_rate': None,
             'independent_samples': None, 'avg_excess_return': None}
    if not rows:
        return empty
    with_base = [r for r in rows if r.review_baseline is not None]
    out = dict(empty)
    out['independent_samples'] = len({_cluster_key(r) for r in rows})
    if with_base:
        base = sum(r.review_baseline for r in with_base) / len(with_base)
        rate = sum(1 for r in with_base if r.review_hit) / len(with_base)
        out['baseline_rate'] = round(base, 3)
        # 只在有基线的同一批样本上比，避免分子分母样本不一致
        out['excess_rate'] = round(rate - base, 3)
    rets = [(1 if r.direction == 'up' else -1) * r.excess_return
            for r in rows if r.excess_return is not None]
    if rets:
        out['avg_excess_return'] = round(sum(rets) / len(rets), 3)
    return out


def _auto_tune_enabled():
    return bool(getattr(settings, 'DETECTOR_AUTO_TUNE', False))


def _tune_window_days():
    try:
        days = int(getattr(settings, 'DETECTOR_TUNE_WINDOW_DAYS', TUNE_WINDOW_DAYS))
    except (TypeError, ValueError):
        days = TUNE_WINDOW_DAYS
    return days if days > 0 else TUNE_WINDOW_DAYS


def _betacf(a, b, x):
    """不完全 Beta 函数的连分式（Lentz 法，Numerical Recipes betacf）。"""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-12:
            break
    return h


def _betainc(a, b, x):
    """正则化不完全 Beta 函数 I_x(a, b)，即 Beta(a, b) 分布在 x 处的 CDF。"""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    ln_front = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                + a * math.log(x) + b * math.log(1 - x))
    front = math.exp(ln_front)
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1 - x) / b


def _prob_above(hits, n, p0):
    """Beta(1,1) 先验、n 个（可为小数加权的）伯努利样本下，P(命中率 > p0) 的后验概率。"""
    return 1.0 - _betainc(1.0 + hits, 1.0 + n - hits, min(max(p0, 0.0), 1.0))


def _dim_samples(scan_date):
    """跨日累计的调参样本：{dim_key: [(簇命中率, 簇平均基线), ...]}，每簇一个样本。"""
    since = scan_date - timedelta(days=_tune_window_days())
    rows = DetectorSignal.objects.filter(
        review_version__gte=2,
        review_hit__isnull=False,
        review_baseline__isnull=False,
        cycles=_SEGMENT_HEAD_CYCLES,
        trade_date__gte=since,
        trade_date__lte=scan_date,
    ).only('trade_date', 'direction', 'target_kind', 'review_hit', 'review_baseline', 'dimensions')
    clusters = {}  # (dim, cluster_key) -> [hits, base_sum, count]
    for sig in rows:
        ck = _cluster_key(sig)
        for key, dim in (sig.dimensions or {}).items():
            # 只有同向投票的维度算（每个投票维度都拿整条信号的结果，归因仍有混淆，
            # 这是信号级复盘的固有局限，写在说明里）
            if not isinstance(dim, dict):
                continue
            if dim.get('direction') != sig.direction or not dim.get('available'):
                continue
            acc = clusters.setdefault((key, ck), [0, 0.0, 0])
            acc[0] += 1 if sig.review_hit else 0
            acc[1] += sig.review_baseline
            acc[2] += 1
    samples = {}
    for (key, _ck), (h, b, c) in clusters.items():
        samples.setdefault(key, []).append((h / c, b / c))
    return samples


def _dim_stat(key, pairs):
    n = len(pairs)
    hits = sum(h for h, _b in pairs)
    base = sum(b for _h, b in pairs) / n
    rate = hits / n
    prob = _prob_above(hits, n, base)
    return {
        'dim': key, 'samples': n, 'hit_rate': round(rate, 3), 'baseline': round(base, 3),
        'excess': round(rate - base, 3), 'prob_above': round(prob, 4),
    }


def tune_dims(scan_date, configs):
    """跨日累计、按日聚类、显著性检验后计算各维度目标参数。

    返回 (adjustments, stats)：adjustments 为已生效（开关开启）的调整；stats 为
    每个有样本维度的统计（无论开关、无论是否显著，供 summary/日志）。
    """
    apply = _auto_tune_enabled()
    source = DetectorTuneLog.SOURCE_AUTO if apply else DetectorTuneLog.SOURCE_DRY_RUN
    adjustments, stats = [], []
    for key, pairs in sorted(_dim_samples(scan_date).items()):
        stat = _dim_stat(key, pairs)
        stats.append(stat)
        entry = _adjust_dim(key, stat, configs, apply=apply)
        if not entry:
            continue
        DetectorTuneLog.objects.update_or_create(
            trade_date=scan_date, dim_key=key, source=source,
            defaults={
                'applied': apply,
                'changes': entry['changes'],
                'stats': stat,
                'reason': entry['reason'][:300],
            },
        )
        if apply:
            adjustments.append(entry)
    return adjustments, stats


def _clamp(value, bounds):
    return min(max(value, bounds[0]), bounds[1])


def _tune_param_for(params):
    """维度生效参数里第一个可调的闸门参数（值为 0 表示闸门关闭，不去打开它）。"""
    for name in TUNE_PARAMS:
        value = params.get(name)
        if isinstance(value, (int, float)) and value > 0:
            return name
    return None


def _adjust_dim(key, stat, configs, apply=True):
    """按维度超额命中率计算目标权重与闸门参数（有界、向默认值收缩、显著才调）。

    返回 None（不显著/样本不足/无变化）或 dict(dim, changes, reason, ...)。
    apply=False 时只计算，不写 DetectorDimConfig（演练）。
    """
    n = stat['samples']
    if n < MIN_ADJ_SAMPLES:
        return None
    prob = stat['prob_above']
    if (1 - TUNE_CONFIDENCE) < prob < TUNE_CONFIDENCE:
        return None
    cfg_row = DetectorDimConfig.objects.filter(key=key).first()
    spec = configs.get(key)
    if cfg_row is None or spec is None:
        return None

    defaults = spec.get('defaults') or {}
    current = {**defaults, **(cfg_row.params or {})}
    excess = stat['excess']
    eff = excess * n / (n + TUNE_PRIOR_N)
    changes = {}

    # 权重：默认 1.0（注册表没有权重默认值，种子行一律 1.0）
    old_weight = cfg_row.weight
    new_weight = round(_clamp(math.exp(WEIGHT_GAIN * eff), WEIGHT_BOUNDS), 3)
    if abs(new_weight - old_weight) >= TUNE_MIN_CHANGE.get('weight', 0):
        changes['weight'] = [old_weight, new_weight]

    pname = _tune_param_for(current)
    if pname:
        rule = TUNE_PARAMS[pname]
        default = defaults.get(pname, NOISE_DEFAULTS.get(pname, current[pname]))
        old = current[pname]
        new = round(_clamp(default + rule['slope'] * eff, rule['bounds']), rule.get('digits', 3))
        if abs(new - old) >= TUNE_MIN_CHANGE.get(pname, 0):
            changes[pname] = [old, new]

    if not changes:
        return None
    verdict = '显著跑赢' if prob >= TUNE_CONFIDENCE else '显著跑输'
    reason = (
        f"{n} 个独立样本命中率 {stat['hit_rate']:.0%}，{verdict}基线 {stat['baseline']:.0%}"
        f"（P(高于基线)={prob:.3f}），按收缩后超额 {eff:+.3f} 设定"
        + '、'.join(f"{k} {v[0]}→{v[1]}" for k, v in changes.items())
    )
    if apply:
        params = dict(cfg_row.params or {})
        if 'weight' in changes:
            cfg_row.weight = changes['weight'][1]
        if pname and pname in changes:
            params[pname] = changes[pname][1]
        cfg_row.params = params
        cfg_row.save(update_fields=['weight', 'params', 'updated_at'])
    return {
        'dim': key,
        'samples': n,
        'hit_rate': stat['hit_rate'],
        'baseline': stat['baseline'],
        'excess': stat['excess'],
        'prob_above': prob,
        'changes': changes,
        # 兼容旧流水读法（from/to_weight）
        'from_weight': old_weight,
        'to_weight': changes.get('weight', [old_weight, old_weight])[1],
        'reason': reason,
    }
