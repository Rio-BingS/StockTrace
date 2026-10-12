"""探测器候选开关（试验区）：否决/市场状态开关原型。

为什么单独成文件：这里的开关都是「回测证明有效再上线」的候选
（见 detector-research/00_plan.md 阶段 4），默认关闭；与 dimensions.py 的
线上维度隔开，阈值方向的改动不会互相踩踏。原先的量价相关性候选维度
vp_corr 经多轮离线验证无效，已删除（det-v2）。

约定：
- 否决/状态开关返回 {available, veto|state, note}，不投票、只供投票汇总层
  （signals.evaluate_target，calm-vote 方向所有）在集成时决定是否接入；
- 数据不足一律 available=False，绝不补 0。
"""

from __future__ import annotations

from datetime import date as _date

from .dimensions import (
    _std_strict,
)

# ============================================================
# 否决 / 市场状态开关原型（默认关闭，不接入投票）
# ============================================================
# 返回约定：{'available': bool, 'veto': 'up' | 'down' | None, 'note': str}
# veto='up' 表示「本对象当日的 up 信号应被否决」；状态开关额外带 'state'。
# 是否以及如何接入 signals.evaluate_target 由集成方决定（calm-vote 方向所有）。

VETO_DEFAULTS = {
    'holder_surge': {
        'enabled': False,
        # 户数环比增幅（%）≥ 该值视为「筹码分散/散户涌入」，否决 up
        'surge_pct': 10.0,
        # 披露（可得）日期距 as_of 超过该自然日数的户数数据视为过期，不否决
        'max_age_days': 100,
    },
    'etf_regime': {
        'enabled': False,
        'window': 5,          # 份额/指数变化的观察窗口（交易日）
        'z_thr': 2.0,         # 宽基 ETF 合计份额 window 日变化的自身历史 z 阈值
        'hist_min': 40,       # z 基线最少样本（份额快照需积累约两个月）
        'index_move_pct': 2.0,  # 同窗口指数涨跌幅门槛（%），与份额反向才算逆周期操作
    },
}


def _veto_result(available, veto, note, **extra):
    return {'available': available, 'veto': veto, 'note': note, **extra}


def veto_holder_surge(ctx, params=None):
    """股东户数激增否决（个股）：最近一次可得的户数环比增幅 ≥ surge_pct 时否决 up。

    依据（02 §2）：户数激增组下月超额显著为负（源达 2014-2026 RankIC 显著为负，
    taolike D10 −7.7%）；但户数下降组并不给正收益——所以只作否决，不加分，
    也不否决 down。2018 单边熊市方向反转（源达），集成时宜配合市场状态。

    数据契约：ctx['holder_counts'] = [{'date': 可得日期 ISO, 'count': 户数}] 升序。
    date 必须是**披露/可得日期**（定期报告或互动易发布日），不是统计截止日，
    否则就是前视。本站现无该数据源：akshare stock_zh_a_gdhs_detail_em（东财，
    单股全历史，含公告日）免费可得，接入需在 datasources/ 落地（data-align 方向）。
    """
    params = {**VETO_DEFAULTS['holder_surge'], **(params or {})}
    rows = [r for r in (ctx.get('holder_counts') or [])
            if r.get('date') and r.get('count') and r['date'] <= ctx['as_of']]
    if len(rows) < 2:
        return _veto_result(False, None, '股东户数披露不足两期')
    prev, last = rows[-2]['count'], rows[-1]['count']
    age = (_date.fromisoformat(ctx['as_of'][:10]) - _date.fromisoformat(rows[-1]['date'][:10])).days
    if age > params['max_age_days']:
        return _veto_result(False, None, f'最近一期户数已披露 {age} 天，过期不用')
    chg = (last / prev - 1) * 100
    if chg >= params['surge_pct']:
        return _veto_result(True, 'up', f'股东户数环比 +{chg:.1f}%（筹码分散），否决向上信号', change_pct=chg)
    return _veto_result(True, None, '', change_pct=chg)


def etf_share_totals(rows_by_date, codes):
    """把 etf_share 快照透视成「观察名单合计份额」序列。

    rows_by_date：[(date, payload)] 升序，payload 为快照行列表 [{code, share}]；
    任一观察 ETF 当天缺份额则该日 None（窗口不齐不算，不拿部分合计冒充）。
    返回 [(iso_date, total | None)]。
    """
    codes = tuple(codes)
    out = []
    for d, payload in rows_by_date:
        by_code = {r.get('code'): r.get('share') for r in payload or [] if r.get('code')}
        vals = [by_code.get(c) for c in codes]
        iso = d.isoformat() if hasattr(d, 'isoformat') else str(d)
        out.append((iso, sum(vals) if all(v for v in vals) else None))
    return out


def load_etf_share_totals(codes, days=120):
    """从本站 etf_share 日度快照读观察名单合计份额（沪市可经上交所历史回填）。"""
    from ..models import MarketDailySnapshot

    rows = (
        MarketDailySnapshot.objects.filter(kind=MarketDailySnapshot.KIND_ETF_SHARE)
        .order_by('-trade_date')
        .values_list('trade_date', 'payload')[:days]
    )
    return etf_share_totals(list(reversed(list(rows))), codes)


def etf_regime(totals, index_closes, params=None):
    """宽基 ETF（国家队观察名单）份额逆周期操作 → 市场状态开关（指数层面）。

    依据（02 §2）：汇金经宽基 ETF 的操作明显逆周期——大跌时天量净申购托底，
    过热时大幅减持。未找到「份额变化预测 N 日收益」的系统回测，所以只做
    状态开关：
    - support（托底）：合计份额 window 日变化 z ≥ z_thr 且同期指数跌幅 ≥ index_move_pct
      → 否决指数 down 信号；
    - cooling（降温）：份额变化 z ≤ −z_thr 且同期指数涨幅 ≥ index_move_pct
      → 否决指数 up 信号；
    - 其余 neutral，不否决。
    totals：etf_share_totals 结果；index_closes：与 totals 同日期对齐的指数收盘
    [(iso_date, close)]。z 基线为此前各日的 window 日份额变化率（不含当前）。
    """
    params = {**VETO_DEFAULTS['etf_regime'], **(params or {})}
    k = int(params['window'])
    closes = dict(index_closes)
    vals = [t for _d, t in totals]
    dates = [d for d, _t in totals]
    if len(vals) < k + 2 or vals[-1] is None or vals[-1 - k] is None:
        return _veto_result(False, None, '份额快照窗口不齐', state=None)
    chg = [
        (vals[i] / vals[i - k] - 1) * 100 if i >= k and vals[i] and vals[i - k] else None
        for i in range(len(vals))
    ]
    hist = [c for c in chg[:-1] if c is not None]
    if len(hist) < int(params['hist_min']):
        return _veto_result(False, None, f'份额变化基线不足（{len(hist)}/{params["hist_min"]}）', state=None)
    mean = sum(hist) / len(hist)
    sd = _std_strict(hist)
    c_now, c_then = closes.get(dates[-1]), closes.get(dates[-1 - k])
    if not sd or not c_now or not c_then:
        return _veto_result(False, None, '指数收盘或份额波动不可用', state=None)
    z = (chg[-1] - mean) / sd
    idx_move = (c_now / c_then - 1) * 100
    thr, move = float(params['z_thr']), float(params['index_move_pct'])
    if z >= thr and idx_move <= -move:
        return _veto_result(True, 'down', f'宽基 ETF 份额 {k} 日激增（z={z:.1f}）且指数 {idx_move:+.1f}%：托底状态',
                            state='support', z=z, index_move=idx_move)
    if z <= -thr and idx_move >= move:
        return _veto_result(True, 'up', f'宽基 ETF 份额 {k} 日骤降（z={z:.1f}）且指数 {idx_move:+.1f}%：降温状态',
                            state='cooling', z=z, index_move=idx_move)
    return _veto_result(True, None, '', state='neutral', z=z, index_move=idx_move)


def apply_vetoes(direction, vetoes):
    """集成辅助：任一可用否决命中当前方向则返回 (None, 原因)，否则 (direction, '')。"""
    for v in vetoes or []:
        if v and v.get('available') and direction and v.get('veto') == direction:
            return None, v.get('note', '')
    return direction, ''
