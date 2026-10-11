"""迭代优化：回看 5 个交易日前信号的兑现情况，微调维度权重与灵敏度阈值。

兑现（hit）定义（ALG-05 修订）：
- 窗口：信号日 T 之后连续 5 个交易日（按交易日历校验），后续数据不满 5 日或
  有缺口时保持待回看，不判定——旧实现有 1 根就判定并永久写入；
- 基期：T+1 开盘价（指数/个股）。信号 17:15 收盘后才产出，T 日收盘价不可
  成交（轻度前视）；板块快照只有日涨跌幅、没有开盘价，只能以 T 日收盘为基期；
- 阈值：k × σ20（T 日及之前 20 个交易日的日涨跌幅标准差），按标的自身波动
  缩放；σ 不可得时退回固定 HIT_PCT；
- 判定：窗口内按收盘价逐日推进，有利方向先达到 +阈值 记兑现；不利方向先
  触及 −阈值（MAE 先到）或到期都没到，记未兑现。

调参口径：
- 命中率与同标的「无条件基线」比较：用 T 日之前的历史，以同样的方向、阈值、
  窗口规则，统计任意起点的兑现率；看超额命中率而不是绝对值——旧口径下随机
  游走的高波动股「命中率」超过 60%，会被当成维度太苛刻而自动放宽门槛；
- 同一标的连续多日的 active 信号回看窗口高度重叠，只取信号段首个 active 日
  （cycles == 2）计入调参样本，其余照常判定但不参与调参；
- 样本不足 MIN_ADJ_SAMPLES 不动参数。

为什么所有调整都有界且留痕：
- 自动调参失控会静默改变探测语义（权重全漂到 2.0 / 阈值归零），有界
  （权重 [0.5, 2.0]、阈值 [0.5×, 2×] 默认值）保证最坏情况下系统仍
  接近初始设计；
- 每次调整写入 DetectorRun.adjustments（who/when/from/to/samples），
  出问题时可回溯，也让人可以否决不合理的自动调整。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from ..market.snapshots import _recent_trading_days
from ..models import DailyQuote, DetectorDimConfig, DetectorSignal
from .dimensions import _std, load_sector_snapshots

logger = logging.getLogger(__name__)

# 回看窗口与兑现口径（见模块 docstring）
REVIEW_LOOKBACK = 5
HIT_SIGMA_K = 1.0       # 兑现阈值 = k × σ20
SIGMA_WINDOW = 20       # σ 取 T 日及之前 20 个日涨跌幅
HIT_PCT = 1.5           # σ 不可得时的兜底阈值（%）
BASELINE_DAYS = 60      # 无条件基线的起点数上限（T 日之前）
MIN_BASELINE_WINDOWS = 20
# 待回看信号只追溯这么多自然日：数据缺口长期补不齐的信号不再每天重试
REVIEW_MAX_AGE_DAYS = 60
# 样本不足不动参数；同标的连续信号只计段首，样本量门槛相应提高
MIN_ADJ_SAMPLES = 10
# 有界调整：步长 5%，权重/阈值的绝对边界
STEP = 1.05
WEIGHT_BOUNDS = (0.5, 2.0)
MIN_SCALE_BOUNDS = (0.5, 2.0)
# 超额命中率（命中率 − 基线）：±WEIGHT_BAND 之外才调权重；低于 EXCESS_LOW →
# 投票太宽松（提高阈值），高于 EXCESS_HIGH → 太苛刻（放宽）
WEIGHT_BAND = 0.05
EXCESS_LOW, EXCESS_HIGH = -0.15, 0.15
# 段首 active 信号的周期数（周期 1 是 watching，2 是首个 active 日）
_SEGMENT_HEAD_CYCLES = 2


def _next_trading_days(after, n):
    """after 之后（不含）的 n 个交易日，升序；日历给不出结论（None/异常）返回 None。"""
    from ..services import is_trading_day

    days, day = [], after
    for _ in range(n * 3 + 15):
        day += timedelta(days=1)
        try:
            flag = is_trading_day(day)
        except Exception:  # noqa: BLE001
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


def _baseline(bars, direction, thr, base_is_open):
    """无条件基线：用信号日之前的历史，任意起点按同规则的兑现率。

    bars: 信号日 T 及之前的 [(date, open, close)] 升序（不含 T 之后，避免前视）。
    起点 s 的窗口 s+1..s+5 必须完全落在 T 及之前。样本窗口不足返回 None。
    """
    n = len(bars)
    last_start = n - 1 - REVIEW_LOOKBACK
    first_start = max(0, last_start - BASELINE_DAYS + 1)
    hits = total = 0
    for s in range(first_start, last_start + 1):
        window = bars[s + 1:s + 1 + REVIEW_LOOKBACK]
        base = window[0][1] if base_is_open else bars[s][2]
        if not base or any(c is None for _d, _o, c in window):
            continue
        hit, _d, _m = _judge(base, [(d, c) for d, _o, c in window], direction, thr)
        hits += hit
        total += 1
    if total < MIN_BASELINE_WINDOWS:
        return None
    return hits / total


def _evaluate_bars(signal, before, after, base_is_open):
    """统一判定：before = T 及之前的 bars（末根必须是 T），after = T 之后的 bars。

    返回 None（保持待回看）或 dict(hit, hit_date, mfe, thr, baseline)。
    """
    if not before or before[-1][0] != signal.trade_date:
        return None
    expected = _next_trading_days(signal.trade_date, REVIEW_LOOKBACK)
    window = after[:REVIEW_LOOKBACK]
    # 后续必须是连续的 5 个交易日（数据缺口/不足都保持待回看）
    if expected is None or [b[0] for b in window] != expected:
        return None
    if any(c is None for _d, _o, c in window):
        return None
    base = window[0][1] if base_is_open else before[-1][2]
    if not base:
        return None
    sigma = _sigma([c for _d, _o, c in before])
    thr = HIT_SIGMA_K * sigma if sigma else HIT_PCT
    hit, hit_date, mfe = _judge(base, [(d, c) for d, _o, c in window], signal.direction, thr)
    return {
        'hit': hit, 'hit_date': hit_date, 'mfe': mfe, 'thr': thr,
        'baseline': _baseline(before, signal.direction, thr, base_is_open),
    }


def _stock_outcome(signal, _cache):
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

    return _evaluate_bars(signal, conv(before), conv(after), base_is_open=True)


def _index_outcome(signal, _cache):
    from datetime import date as _date

    from ..market import indices

    try:
        data = indices.fetch_index_daily_series(signal.target_code, days=130)
    except Exception:  # noqa: BLE001
        return None
    if not data.get('available'):
        return None
    bars = [(_date.fromisoformat(it['date']), it.get('open'), it.get('close'))
            for it in data['items']]
    before = [b for b in bars if b[0] <= signal.trade_date]
    after = [b for b in bars if b[0] > signal.trade_date]
    return _evaluate_bars(signal, before, after, base_is_open=True)


def _sector_outcome(signal, cache):
    """板块：快照涨跌幅链式合成伪价格（基期任意），以 T 日收盘为基期（无开盘价）。"""
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
    return _evaluate_bars(signal, before, after, base_is_open=False)


def review_pending(scan_date, configs):
    """回看并调参。返回 summary（写进 DetectorRun）。

    只回看 status=active 的信号：watching（周期 1）从未满足「维持」，
    不算一次真正的信号假设，不参与命中率统计，也不调参。
    """
    days = _recent_trading_days(REVIEW_LOOKBACK + 1, end_date=scan_date)
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
    dim_stats = {}  # key -> {'hit': n, 'miss': n, 'base': 基线命中率之和}
    adjustments = []
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
        sig.save(update_fields=['review_hit', 'hit_date', 'reviewed_at', 'status', 'updated_at'])
        reviewed += 1
        hits += 1 if hit else 0
        baseline = outcome['baseline']
        if baseline is not None:
            baselines.append(baseline)

        # 只有信号段首日、且有基线可比的样本参与调参（见模块 docstring）
        if sig.cycles != _SEGMENT_HEAD_CYCLES or baseline is None:
            continue
        # 统计每个投票维度在命中/未兑现信号里的表现（只有同向投票的维度算）
        for key, dim in (sig.dimensions or {}).items():
            if dim.get('direction') == sig.direction and dim.get('available'):
                stat = dim_stats.setdefault(key, {'hit': 0, 'miss': 0, 'base': 0.0})
                stat['hit' if hit else 'miss'] += 1
                stat['base'] += baseline

    for key, stat in dim_stats.items():
        entry = _adjust_dim(key, stat, configs)
        if entry:
            adjustments.append(entry)

    return {
        'reviewed': reviewed,
        'hits': hits,
        'hit_rate': round(hits / reviewed, 3) if reviewed else None,
        'baseline_rate': round(sum(baselines) / len(baselines), 3) if baselines else None,
        'adjustments': adjustments,
    }


def _adjust_dim(key, stat, configs):
    """按单维度超额命中率（命中率 − 同标的基线）微调权重与灵敏度阈值（有界）。"""
    n = stat['hit'] + stat['miss']
    if n < MIN_ADJ_SAMPLES:
        return None
    cfg_row = DetectorDimConfig.objects.filter(key=key).first()
    spec = configs.get(key)
    if cfg_row is None or spec is None:
        return None

    rate = stat['hit'] / n
    baseline = stat['base'] / n
    excess = rate - baseline
    default_min = spec['defaults'].get('min')
    old_weight = cfg_row.weight
    old_min = (cfg_row.params or {}).get('min', default_min)
    new_weight, new_min = old_weight, old_min
    reasons = []

    # 权重：投票的信号明显跑赢基线 → 加权；明显跑输 → 减权
    if excess > WEIGHT_BAND:
        new_weight = min(WEIGHT_BOUNDS[1], round(old_weight * STEP, 3))
        reasons.append(f"命中率 {rate:.0%} 高于基线 {baseline:.0%}，权重上调")
    elif excess < -WEIGHT_BAND:
        new_weight = max(WEIGHT_BOUNDS[0], round(old_weight / STEP, 3))
        reasons.append(f"命中率 {rate:.0%} 低于基线 {baseline:.0%}，权重下调")

    # 阈值（min）：大幅跑输基线 = 投票太宽松 → 提高门槛；大幅跑赢 = 太苛刻 → 放宽
    if default_min:
        if excess < EXCESS_LOW:
            new_min = min(old_min * STEP, default_min * MIN_SCALE_BOUNDS[1])
            reasons.append('超额命中率过低，提高投票门槛')
        elif excess > EXCESS_HIGH:
            new_min = max(old_min / STEP, default_min * MIN_SCALE_BOUNDS[0])
            reasons.append('超额命中率过高，放宽投票门槛')
        else:
            new_min = old_min

    params = dict(cfg_row.params or {})
    changed = False
    if abs(new_weight - old_weight) > 1e-9:
        cfg_row.weight = new_weight
        changed = True
    if default_min and abs(new_min - old_min) > 1e-9:
        params['min'] = round(new_min, 6)
        cfg_row.params = params
        changed = True
    if not changed:
        return None
    cfg_row.save(update_fields=['weight', 'params', 'updated_at'])
    return {
        'dim': key,
        'samples': n,
        'hit_rate': round(rate, 3),
        'baseline': round(baseline, 3),
        'excess': round(excess, 3),
        'from_weight': old_weight,
        'to_weight': cfg_row.weight,
        'from_min': old_min,
        'to_min': params.get('min', old_min),
        'reason': '；'.join(reasons),
    }
