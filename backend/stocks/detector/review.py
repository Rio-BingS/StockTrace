"""迭代优化：回看 5 个交易日前信号的兑现情况，微调维度权重与灵敏度阈值。

兑现（hit）定义：信号方向上，其后 5 个交易日内出现过 ≥ HIT_PCT 的
最大有利波动（指数/个股按真实收盘价，板块按真实涨跌幅链值）。
温和兑现（+1.5%）而非大涨：探测器捕捉的是「早期迹象是否成立」，
不是预测大行情。

为什么所有调整都有界且留痕：
- 自动调参失控会静默改变探测语义（权重全漂到 2.0 / 阈值归零），有界
  （权重 [0.5, 2.0]、阈值 [0.5×, 2×] 默认值）保证最坏情况下系统仍
  接近初始设计；
- 每次调整写入 DetectorRun.adjustments（who/when/from/to/samples），
  出问题时可回溯，也让人可以否决不合理的自动调整。
"""

from __future__ import annotations

import logging

from django.utils import timezone

from ..market.snapshots import _recent_trading_days
from ..models import DailyQuote, DetectorDimConfig, DetectorSignal, MarketDailySnapshot

logger = logging.getLogger(__name__)

# 回看窗口与兑现阈值（口径见模块 docstring）；样本不足不动参数
REVIEW_LOOKBACK = 5
HIT_PCT = 1.5
MIN_ADJ_SAMPLES = 3
# 有界调整：步长 5%，权重/阈值的绝对边界
STEP = 1.05
WEIGHT_BOUNDS = (0.5, 2.0)
MIN_SCALE_BOUNDS = (0.5, 2.0)
# 命中率低于 20% → 该维度投票太宽松（提高阈值）；高于 60% → 太苛刻（放宽）
HIT_RATE_LOW, HIT_RATE_HIGH = 0.2, 0.6


def _stock_move(signal):
    from ..models import Stock

    stock = Stock.objects.filter(code=signal.target_code).first()
    if stock is None:
        return None, None
    rows = list(
        # 只用已收盘日线（DAT-13）：盘中当日 bar 的收盘价是盘中价，算出的
        # 前瞻收益会随拉取时刻漂移，不能用来调权重
        DailyQuote.objects.filter(stock=stock, trade_date__gt=signal.trade_date, is_final=True)
        .order_by('trade_date')
        .values_list('trade_date', 'close_price')[:REVIEW_LOOKBACK]
    )
    if not rows or signal.score is None:
        return None, None
    base = (
        DailyQuote.objects.filter(stock=stock, trade_date=signal.trade_date, is_final=True)
        .values_list('close_price', flat=True)
        .first()
    )
    if not base:
        return None, None
    return _favorable(float(base), [(r[0], float(r[1])) for r in rows], signal.direction)


def _index_move(signal):
    from ..market import indices

    try:
        data = indices.fetch_index_daily_series(signal.target_code, days=130)
    except Exception:
        return None, None
    if not data.get('available'):
        return None, None
    items = [it for it in data['items'] if it['date'] > signal.trade_date.isoformat()]
    items = items[:REVIEW_LOOKBACK]
    base = next(
        (it['close'] for it in reversed(data['items']) if it['date'] == signal.trade_date.isoformat()),
        None,
    )
    if base is None or not items:
        return None, None
    pairs = [(it['date'], it['close']) for it in items]
    return _favorable(float(base), pairs, signal.direction)


def _sector_move(signal):
    kind_db = (
        MarketDailySnapshot.KIND_INDUSTRY_FF
        if signal.target_kind == 'industry'
        else MarketDailySnapshot.KIND_CONCEPT_FF
    )
    rows = (
        MarketDailySnapshot.objects.filter(kind=kind_db, trade_date__gte=signal.trade_date)
        .order_by('trade_date')
        .values_list('trade_date', 'payload')[:REVIEW_LOOKBACK + 1]
    )
    rows = list(rows)
    if not rows:
        return None, None
    # 信号日当天的涨跌幅是基期的一部分吗？兑现定义为「其后」的变化，
    # 所以从信号日的下一个有值日期开始链乘。
    pcts = []
    for trade_date, payload in rows:
        if trade_date == signal.trade_date:
            continue
        row = next((r for r in (payload or []) if r.get('name') == signal.target_code), None)
        if row is None or row.get('change_pct') is None:
            return None, None
        pcts.append((trade_date, row['change_pct']))
    if not pcts:
        return None, None
    prod = 1.0
    move = None
    hit_date = None
    for trade_date, pct in pcts:
        prod *= 1 + pct / 100
        cumulative = (prod - 1) * 100
        if move is None:
            move = cumulative
            hit_date = trade_date
        elif signal.direction == 'up' and cumulative > move:
            move, hit_date = cumulative, trade_date
        elif signal.direction == 'down' and cumulative < move:
            move, hit_date = cumulative, trade_date
    return move, hit_date


def _favorable(base, pairs, direction):
    """价格序列上的最大有利波动：up 取最大涨幅、down 取最大跌幅。"""
    move, hit_date = None, None
    for trade_date, close in pairs:
        if not close:
            continue
        pct = (close / base - 1) * 100
        if move is None:
            move, hit_date = pct, trade_date
        elif direction == 'up' and pct > move:
            move, hit_date = pct, trade_date
        elif direction == 'down' and pct < move:
            move, hit_date = pct, trade_date
    if move is None:
        return None, None
    return move, hit_date


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
        )
    )

    reviewed, hits = 0, 0
    dim_stats = {}  # key -> {'hit': n, 'miss': n}
    adjustments = []

    for sig in pending:
        if sig.target_kind == 'stock':
            move, hit_date = _stock_move(sig)
        elif sig.target_kind == 'index':
            move, hit_date = _index_move(sig)
        else:
            move, hit_date = _sector_move(sig)
        if move is None:
            continue
        hit = (move >= HIT_PCT) if sig.direction == 'up' else (move <= -HIT_PCT)
        sig.review_hit = hit
        sig.hit_date = hit_date if hit else None
        sig.reviewed_at = timezone.now()
        sig.status = (
            DetectorSignal.STATUS_CONFIRMED if hit else DetectorSignal.STATUS_MISSED
        )
        sig.save(update_fields=['review_hit', 'hit_date', 'reviewed_at', 'status', 'updated_at'])
        reviewed += 1
        hits += 1 if hit else 0

        # 统计每个投票维度在命中/未兑现信号里的表现（只有同向投票的维度算）
        for key, dim in (sig.dimensions or {}).items():
            if dim.get('direction') == sig.direction and dim.get('available'):
                stat = dim_stats.setdefault(key, {'hit': 0, 'miss': 0})
                stat['hit' if hit else 'miss'] += 1

    for key, stat in dim_stats.items():
        entry = _adjust_dim(key, stat, configs)
        if entry:
            adjustments.append(entry)

    return {
        'reviewed': reviewed,
        'hits': hits,
        'hit_rate': round(hits / reviewed, 3) if reviewed else None,
        'adjustments': adjustments,
    }


def _adjust_dim(key, stat, configs):
    """按单维度命中率微调权重与灵敏度阈值（有界），返回调整流水条目或 None。"""
    n = stat['hit'] + stat['miss']
    if n < MIN_ADJ_SAMPLES:
        return None
    cfg_row = DetectorDimConfig.objects.filter(key=key).first()
    spec = configs.get(key)
    if cfg_row is None or spec is None:
        return None

    rate = stat['hit'] / n
    default_min = spec['defaults'].get('min')
    old_weight = cfg_row.weight
    old_min = (cfg_row.params or {}).get('min', default_min)
    new_weight, new_min = old_weight, old_min
    reasons = []

    # 权重：命中的信号里该维度投了票 → 加权；未兑现占多 → 减权
    if stat['hit'] > stat['miss']:
        new_weight = min(WEIGHT_BOUNDS[1], round(old_weight * STEP, 3))
        reasons.append(f"命中率 {rate:.0%} 高于未兑现，权重上调")
    elif stat['miss'] > stat['hit']:
        new_weight = max(WEIGHT_BOUNDS[0], round(old_weight / STEP, 3))
        reasons.append(f"命中率 {rate:.0%} 低于未兑现，权重下调")

    # 阈值（min）：命中率过低 = 投票太宽松 → 提高门槛；过高 = 太苛刻 → 放宽
    if default_min:
        if rate < HIT_RATE_LOW:
            new_min = min(old_min * STEP, default_min * MIN_SCALE_BOUNDS[1])
            reasons.append('命中率过低，提高投票门槛')
        elif rate > HIT_RATE_HIGH:
            new_min = max(old_min / STEP, default_min * MIN_SCALE_BOUNDS[0])
            reasons.append('命中率过高，放宽投票门槛')
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
        'from_weight': old_weight,
        'to_weight': cfg_row.weight,
        'from_min': old_min,
        'to_min': params.get('min', old_min),
        'reason': '；'.join(reasons),
    }
