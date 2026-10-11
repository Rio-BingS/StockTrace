"""平静状态判定：探测器的前置闸门——量能平稳、振幅低位的对象才进入微变监测。

为什么做成独立闸门而不是一个维度：任务定义的探测目标是「成交量处于
相对平稳区间时，监测其他维度的渐进变化」。平静是前提条件（过滤宇宙），
不是投票维度——把它放进投票会稀释「≥2 维同向」的方向性语义。

阈值依据（平静参数不参与自动迭代，理由在文件尾）：
- RVOL 0.7~1.5：任务明确给定的平静区间；
- 量能波动收窄 / 振幅低位：取自身历史的 40 分位——「低位」必须是相对
  自己的历史而言（高波动股与低波动股的「平静」标准不同）。三个条件同时
  成立，随机游走模拟下单个对象约 14%（指数/个股）、约 21%（板块）的交易日
  通过闸门（旧注释「约六成时间有候选对象」与模拟不符，ALG-04 更正）。
"""

from __future__ import annotations

from .dimensions import _mean, _percentile, _std

# 指数/个股：成交量口径的平静判定参数
PRICE_CALM_PARAMS = {
    'rvol_min': 0.7,     # 当日量 / 前10日均量 下限
    'rvol_max': 1.5,     # 上限：超过 1.5 已经「不平静」
    'lookback': 60,      # 自身历史分位的取样窗口
    'vol_cv_pct': 0.4,   # 近5日量能变异系数 ≤ 自身历史 40 分位 → 收窄
    'amp_pct': 0.4,      # 当日振幅 ≤ 自身历史 40 分位 → 振幅低位
}
# 板块：快照口径（无成交量）的平静判定参数
SECTOR_CALM_PARAMS = {
    'window': 5,
    'flow_pct': 0.5,   # 近3日平均|净额| ≤ 自身历史中位数 → 资金面平稳
    'chg_pct': 0.4,    # 近5日涨跌幅标准差 ≤ 自身历史 40 分位 → 波动低位
}


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


def is_calm_price(ctx, params=None):
    """指数/个股平静判定：量比居中 + 量能波动收窄 + 振幅低位。

    返回 (is_calm, metrics)：metrics 里带未达标原因（首条），供 run 日志
    聚合统计「为什么今天没有候选对象」。
    """
    params = {**PRICE_CALM_PARAMS, **(params or {})}
    volumes, reasons = ctx.get('volume') or [], []

    rvol = _rvol(volumes, len(volumes) - 1)
    if rvol is None:
        return False, {'reason': '成交量历史不足，无法判定平静'}
    if not (params['rvol_min'] <= rvol <= params['rvol_max']):
        reasons.append(f'RVOL={rvol:.2f} 超出平静区间')

    # 量能波动收窄：近5日量的变异系数(CV) 对比自身滚动 CV 的分位
    # （5 日窗口是「周尺度波动」的固定口径，与维度参数无关，故不进 params）
    k = 5
    n = len(volumes)
    lookback = params['lookback']
    cvs = []
    # 基线只取「当前窗口之前」的滚动窗口（ALG-10）：range 到 n（不含）——
    # 下标 i 的窗口是 volumes[i-k:i]，i=n 就是当前窗口，旧实现把当前值算进了
    # 自己的分位基线，让「≤40 分位」判定偏宽
    start = max(k, n - lookback)
    for i in range(start, n):
        win = volumes[max(0, i - k):i]
        m = _mean(win)
        if m and all(v is not None for v in win):
            cvs.append(_std(win) / m)
    # 当前窗口同样要求 5 根量齐全：只剩 1 个非 None 时 _std 为 None，直接除会 TypeError（P0 ALG-01）
    cur = volumes[-k:]
    cur_mean, cur_std = _mean(cur), _std(cur)
    current_cv = cur_std / cur_mean \
        if len(cur) == k and all(v is not None for v in cur) and cur_mean and cur_std is not None else None
    if current_cv is None or not cvs:
        return False, {'reason': '量能样本不足，无法判定收窄'}
    if current_cv > _percentile(cvs, params['vol_cv_pct']):
        reasons.append('量能波动未收窄')

    # 振幅低位：(high-low)/prev_close 与自身历史比
    # 当前值与基线同口径（ALG-10）：逐日振幅缺失处保留 None，当日振幅必须是
    # 「最后一根」本身（旧实现先丢掉 None 再取 amps[-1]，当日缺数据时会拿前一天
    # 冒充当日）；基线取当日之前的 lookback 根，不含当日
    highs, lows, closes = ctx.get('high'), ctx.get('low'), ctx.get('close')
    amps = []
    if highs and lows and closes:
        for i in range(1, len(closes)):
            h, low, pc = highs[i], lows[i], closes[i - 1]
            amps.append((h - low) / pc * 100 if None not in (h, low, pc) and pc else None)
    if not amps or amps[-1] is None:
        return False, {'reason': '当日振幅缺失，无法判定振幅低位'}
    amp_now = amps[-1]
    amp_base = [a for a in amps[:-1][-lookback:] if a is not None]
    if len(amp_base) < 10:
        return False, {'reason': '振幅样本不足'}
    if amp_now > _percentile(amp_base, params['amp_pct']):
        reasons.append(f'振幅 {amp_now:.2f}% 非低位')

    if reasons:
        return False, {'reason': reasons[0], 'rvol': round(rvol, 3)}
    return True, {'rvol': round(rvol, 3), 'cv': round(current_cv, 3)}


def is_calm_sector(ctx, params=None):
    """板块平静判定（快照口径）：资金面平稳 + 涨跌幅波动低位。

    板块没有成交量序列（快照 payload 只有净额与涨跌幅），「量能平稳」
    用净额波动平稳代理——这是口径替换而非伪造，已在文档中说明。
    """
    params = {**SECTOR_CALM_PARAMS, **(params or {})}
    nets, pcts = ctx.get('net') or [], ctx.get('change_pct') or []
    window = params['window']

    if len(nets) < window + 3:
        return False, {'reason': '净额快照窗口不足'}

    # 近 3 日平均|净额| 对比自身滚动 3 日均值的历史分位；基线不含当前 3 日窗口（ALG-10）
    roll = []
    for i in range(len(nets) - 3):
        win = nets[i:i + 3]
        if all(v is not None for v in win):
            roll.append(_mean([abs(v) for v in win]))
    tail = nets[-3:]
    if any(v is None for v in tail) or not roll:
        return False, {'reason': '净额窗口存在缺失快照'}
    flow_now = _mean([abs(v) for v in tail])
    if flow_now > _percentile(roll, params['flow_pct']):
        return False, {'reason': '资金净额波动未收窄'}

    if len(pcts) < window:
        return False, {'reason': '涨跌幅窗口不足'}
    if any(v is None for v in pcts[-window:]):
        return False, {'reason': '涨跌幅窗口存在缺失快照'}
    chg_std = _std(pcts[-window:])
    if chg_std is None:
        return False, {'reason': '涨跌幅窗口不足'}
    # 基线是当前窗口之前的滚动窗口（ALG-10）：range 到 len(pcts)（不含），
    # 与量能 CV 一样不把当前值算进自己的分位
    roll_std = [
        _std(pcts[i - window:i])
        for i in range(window, len(pcts))
        if all(v is not None for v in pcts[i - window:i])
    ]
    # 回填后头几天每个滚动窗口都含 None → roll_std 为空、分位为 None，float > None 会 TypeError（P0 ALG-01）
    chg_thr = _percentile(roll_std, params['chg_pct'])
    if chg_thr is None:
        return False, {'reason': '涨跌幅滚动样本不足'}
    if chg_std > chg_thr:
        return False, {'reason': '涨跌幅波动非低位'}

    return True, {'flow': round(flow_now, 0), 'chg_std': round(chg_std, 3)}


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
