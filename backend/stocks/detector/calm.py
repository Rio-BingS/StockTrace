"""平静状态判定：探测器的前置闸门——此前平静、价格尚未大动的对象才进入微变监测。

为什么做成独立闸门而不是一个维度：任务定义的探测目标是「成交量处于
相对平稳区间时，监测其他维度的渐进变化」。平静是前提条件（过滤宇宙），
不是投票维度——把它放进投票会稀释同向投票的方向性语义。

regime 化（#2，01_code_audit §3）——旧实现要求「当天」量能 CV 低、RVOL≤1.5、
单日振幅低位，与维度要找的「量能温和抬升」互相矛盾（量比斜率投票率 21%→6%），
单日振幅又让平静状态次日保持率只有约 30%。现在拆成两层：
1. 此前平静（regime）：量能 CV 与平均真实波幅(TR) 在**滞后窗口**（止于
   T−calm_lag）上与自身历史比，近 calm_lag 日的偏离交给维度去测；状态带迟滞——
   进入要两项都 ≤ 40 分位，维持只要都 ≤ exit_pct（60 分位），从 T−hysteresis_days
   起逐日推进（只用 ctx 内 ≤T 的数据，无状态落库、回测可复算）；
2. 价格尚未大动（当日）：近 calm_lag 日累计涨跌 ≤ move_z·σ·√calm_lag、当日 TR
   不在 90 分位之上、RVOL 不出 [0.7, 2.5]——只排除事件式突变，保留温和放量。
这对应外部实证里唯一有正向证据的「量先于价」：量能领先、价格还没动
（02_external_research §1.2）。

振幅改用真实波幅 TR=max(H, 前收)−min(L, 前收)，跳空计入（旧 (H−L)/前收 把
跳空 5% 但日内窄幅的日子算作振幅低位）。

成交量口径（#15 接口）：优先读 ctx['volume_adj']（data-align 方向提供的送转
校正成交量，与 volume 等长），没有时退回 ctx['volume']。
"""

from __future__ import annotations

import math

from .constants import (
    CALM_MIN_AMP_SAMPLES,
    PRICE_CALM_PARAMS,
    SECTOR_CALM_PARAMS,
    SECTOR_FLOW_DAYS,
)
from .dimensions import _mean, _percentile, _std


def _rvol(volumes, i, window=10):
    """第 i 日的量比：当日量 / 前 window 日均量；数据不齐返回 None。

    基期窗口要求齐全（ALG-10）：旧实现用跳过 None 的 _mean，缺几天也照算，
    与下面「量能 CV / 振幅」都要求窗口齐全的口径不一致。
    """
    if i < window or volumes[i] is None:
        return None
    win = volumes[i - window:i]
    if any(v is None for v in win):
        return None
    base = _mean(win)
    if not base:
        return None
    return volumes[i] / base


def calm_volumes(ctx):
    """平静判定用的成交量序列：送转校正量优先（#15 接口），否则原始成交量。"""
    adj = ctx.get('volume_adj')
    if adj and len(adj) == len(ctx.get('volume') or adj):
        return adj
    return ctx.get('volume') or []


def _true_range_pct(highs, lows, closes):
    """逐日真实波幅 TR/前收(%)，与 K 线下标对齐（首根无前收为 None）。"""
    n = len(closes)
    out = [None] * n
    for i in range(1, n):
        h, low, pc = highs[i], lows[i], closes[i - 1]
        if None in (h, low, pc) or not pc:
            continue
        out[i] = (max(h, pc) - min(low, pc)) / pc * 100
    return out


def _rolling(values, k, fn):
    """以下标 e 结尾的 k 日窗口统计量（窗口须齐全），与 values 下标对齐。"""
    out = [None] * len(values)
    for e in range(k - 1, len(values)):
        win = values[e - k + 1:e + 1]
        if all(v is not None for v in win):
            out[e] = fn(win)
    return out


def _cv(win):
    m, s = _mean(win), _std(win)
    return s / m if m and s is not None else None


def _base(metric, e, lookback):
    """下标 e 的分位基线：e 之前（不含 e）最多 lookback 个窗口值，不含当前窗口。"""
    return [v for v in metric[max(0, e - lookback):e] if v is not None]


def _regime(metrics, n, params):
    """迟滞状态机：返回 (T 日是否处于平静 regime, 连续平静天数, 首条未达标原因)。

    metrics: [(名称, 与 K 线对齐的窗口统计序列, 进入分位)]；第 t 日看的是止于
    t−lag 的滞后窗口，基线是该窗口之前的同口径窗口（不含自身，ALG-10）。
    """
    lag, lookback = params['calm_lag'], params['lookback']
    exit_pct, min_n = params['exit_pct'], params['regime_min_samples']
    state, streak, reason = False, 0, '此前平静状态样本不足'
    for t in range(max(0, n - 1 - params['hysteresis_days']), n):
        e = t - lag
        if e < 0:
            continue
        bad = None
        for name, metric, enter_pct in metrics:
            cur = metric[e]
            if cur is None:
                bad = (f'{name}窗口存在缺失', True)
                break
            base = _base(metric, e, lookback)
            if len(base) < min_n:
                bad = ('此前平静状态样本不足', True)
                break
            # 维持用放宽的 exit_pct，进入用各自的 enter_pct
            thr = _percentile(base, exit_pct if state else enter_pct)
            if cur > thr:
                bad = (f'此前{name}非低位', False)
                break
        if bad is None:
            state = True
            streak += 1
        else:
            state, streak, reason = False, 0, bad[0]
    return state, streak, reason


def _moved_too_far(closes_or_pcts, lag, move_z, lookback, from_pct=False):
    """价格尚未大动：近 lag 日累计涨跌 vs move_z·σ·√lag（σ 取此前日涨跌幅）。

    返回 (是否已大动, 累计涨跌%, 阈值%)；样本不足返回 (None, None, None)。
    """
    if from_pct:
        rets = list(closes_or_pcts)
    else:
        c = closes_or_pcts
        rets = [
            (c[i] / c[i - 1] - 1) * 100 if c[i] is not None and c[i - 1] else None
            for i in range(1, len(c))
        ]
    recent = rets[-lag:]
    hist = [r for r in rets[:-lag][-lookback:] if r is not None]
    if len(recent) < lag or any(r is None for r in recent) or len(hist) < CALM_MIN_AMP_SAMPLES:
        return None, None, None
    sigma = _std(hist)
    if sigma is None:
        return None, None, None
    move = (math.prod(1 + r / 100 for r in recent) - 1) * 100
    # σ=0（长期一字不动）时任何非零漂移都算「已大动」，零漂移不算
    thr = move_z * sigma * math.sqrt(lag)
    return abs(move) > thr, move, thr


def is_calm_price(ctx, params=None):
    """指数/个股平静判定：此前平静（滞后窗口 + 迟滞）+ 当日价格尚未大动。

    返回 (is_calm, metrics)：metrics 里带未达标原因（首条），供 run 日志
    聚合统计「为什么今天没有候选对象」。
    """
    params = {**PRICE_CALM_PARAMS, **(params or {})}
    volumes = calm_volumes(ctx)
    n = len(volumes)

    rvol = _rvol(volumes, n - 1, params['rvol_window']) if n else None
    if rvol is None:
        return False, {'reason': '成交量历史不足，无法判定平静'}

    highs, lows, closes = ctx.get('high'), ctx.get('low'), ctx.get('close')
    if not (highs and lows and closes):
        return False, {'reason': '当日振幅缺失，无法判定振幅低位'}
    # 各序列按尾部对齐（最后一根都是 T）：生产 ctx 等长，手工/回填序列可能不等长
    m = min(n, len(highs), len(lows), len(closes))
    volumes, highs, lows, closes = volumes[-m:], highs[-m:], lows[-m:], closes[-m:]
    n = m
    trs = _true_range_pct(highs, lows, closes)
    if trs[-1] is None:
        return False, {'reason': '当日振幅缺失，无法判定振幅低位'}

    k = params['cv_window']
    cvs = _rolling(volumes, k, _cv)
    tr_means = _rolling(trs, k, _mean)
    state, streak, reason = _regime(
        [('量能波动', cvs, params['vol_cv_pct']), ('振幅(TR)', tr_means, params['amp_pct'])],
        n, params,
    )
    if not state:
        return False, {'reason': reason, 'rvol': round(rvol, 3)}

    # ---- 当日：价格尚未大动（只排除突变，温和偏离留给维度）----
    if not (params['rvol_min'] <= rvol <= params['rvol_max']):
        return False, {'reason': f'RVOL={rvol:.2f} 超出平静区间', 'rvol': round(rvol, 3)}
    moved, move, thr = _moved_too_far(closes, params['calm_lag'], params['move_z'], params['lookback'])
    if moved is None:
        return False, {'reason': '价格样本不足，无法判定是否已大动'}
    if moved:
        return False, {'reason': f'近{params["calm_lag"]}日累计涨跌 {move:.2f}% 已大动',
                       'rvol': round(rvol, 3)}

    tr_base = [v for v in trs[:-1][-params['lookback']:] if v is not None]
    if len(tr_base) < CALM_MIN_AMP_SAMPLES:
        return False, {'reason': '振幅样本不足'}
    if trs[-1] > _percentile(tr_base, params['tr_spike_pct']):
        return False, {'reason': f'当日真实波幅 {trs[-1]:.2f}% 异常放大', 'rvol': round(rvol, 3)}
    return True, {
        'rvol': round(rvol, 3),
        'cv': round(cvs[n - 1 - params['calm_lag']], 3),
        'tr': round(trs[-1], 3),
        'regime_days': streak,
    }


def is_calm_sector(ctx, params=None):
    """板块平静判定（快照口径）：此前资金面平稳 + 涨跌幅波动低位（regime）+ 未大动。

    板块没有成交量序列（快照 payload 只有净额与涨跌幅），「量能平稳」
    用净额波动平稳代理——这是口径替换而非伪造，已在文档中说明。
    """
    params = {**SECTOR_CALM_PARAMS, **(params or {})}
    params.setdefault('lookback', PRICE_CALM_PARAMS['lookback'])
    nets, pcts = ctx.get('net') or [], ctx.get('change_pct') or []
    window, flow_days = params['window'], SECTOR_FLOW_DAYS

    if len(nets) < window + flow_days:
        return False, {'reason': '净额快照窗口不足'}
    if any(v is None for v in nets[-flow_days:]):
        return False, {'reason': '净额窗口存在缺失快照'}
    if len(pcts) < window:
        return False, {'reason': '涨跌幅窗口不足'}
    if any(v is None for v in pcts[-window:]):
        return False, {'reason': '涨跌幅窗口存在缺失快照'}
    if len(pcts) != len(nets):
        return False, {'reason': '净额与涨跌幅序列长度不一致'}

    abs_nets = [abs(v) if v is not None else None for v in nets]
    flows = _rolling(abs_nets, flow_days, _mean)
    chg_stds = _rolling(pcts, window, _std)
    state, streak, reason = _regime(
        [('资金净额', flows, params['flow_pct']), ('涨跌幅波动', chg_stds, params['chg_pct'])],
        len(nets), params,
    )
    if not state:
        if reason == '此前平静状态样本不足':
            reason = '涨跌幅滚动样本不足'
        return False, {'reason': reason}

    # ---- 当日：资金突变 / 价格已大动 才排除 ----
    flow_now = flows[-1]
    flow_base = _base(flows, len(flows) - 1, params['lookback'])
    if flow_base and flow_now > _percentile(flow_base, params['flow_spike_pct']):
        return False, {'reason': '资金净额突增，不是渐进变化'}
    moved, move, _thr = _moved_too_far(
        pcts, params['calm_lag'], params['move_z'], params['lookback'], from_pct=True)
    if moved is None:
        return False, {'reason': '涨跌幅滚动样本不足'}
    if moved:
        return False, {'reason': f'近{params["calm_lag"]}日累计涨跌 {move:.2f}% 已大动'}

    return True, {
        'flow': round(flow_now, 0),
        'chg_std': round(chg_stds[len(nets) - 1 - params['calm_lag']], 3),
        'regime_days': streak,
    }


def is_calm(ctx, params=None):
    """按对象类型分派的平静判定入口。"""
    if ctx['kind'] in ('index', 'stock'):
        return is_calm_price(ctx, params)
    return is_calm_sector(ctx, params)


# 为什么平静参数不参与 review 的自动迭代：迭代优化的反馈信号是「信号
# 兑现与否」，而平静闸门决定了「今天有没有候选对象」——拿兑现率去调
# 闸门会让扫描宇宙随命中率漂移（命中率低→放宽平静→更多弱候选→命中率
# 更低），形成正反馈失控。维度参数（min/权重）在固定宇宙内调整才是
# 可归因的。因此平静参数只在代码/配置层人工调整。
