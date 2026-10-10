"""信号判定：维度同向投票 → 方向/得分/周期 → 严重程度分级 → 幂等落库。

判定规则（对应任务定义）：
- 至少 2 个微变维度同向变化：同侧票数 ≥ 2；
- 且对侧 0 票：存在反向票说明维度间分歧，不构成方向性微变（口径从严，
  宁可少报不误报——探测器定位是「早期迹象」，误报比漏报更伤信任）；
- 连续维持 2-3 个周期：周期数按「连续交易日存在同方向信号行」累计，
  周期 1 落库为 watching（观察中），≥2 才是 active（维持中）并在列表突出；
- 幅度温和：由各维度在评估时的 [min, max] 温和带保证（突变被排除在
  投票之外），这里不再重复判断。
"""

from __future__ import annotations

from datetime import date as _date, timedelta

from ..models import DetectorSignal

# 同向投票的最少维度数与「零反向票」约束（见模块 docstring 的取舍说明）
VOTE_MIN_DIMS = 2

# 严重程度分级阈值：score 是同侧维度 |value| 的加权平均（0-100），
# 55/40 两条线让「多维度共振+多周期维持」显著高于「勉强两票」。
SEVERITY_RULES = {'high_score': 55.0, 'medium_score': 40.0}


def _as_date(value):
    """ctx['as_of'] 是 ISO 字符串（序列里带出来的），周期回看需要 date 运算。"""
    return _date.fromisoformat(value) if isinstance(value, str) else value


def _count_cycles(target_kind, target_code, direction, before_date):
    """信号日之前连续多少个交易日存在同方向信号行（周期维持计数）。

    为什么按信号行而非「微变状态」计数：状态没有被持久化，信号行本身就是
    每日状态快照；中间某天扫描没跑（宕机/上游全挂）周期自然中断——维持
    本来就要求逐日确认，中断后重新累计是符合语义的保守行为。
    交易日历不可用时按自然日推进：可能低估连续性，宁可少算不多算。
    """
    from ..services import is_trading_day

    cycles = 0
    day = before_date
    for _ in range(20):
        day -= timedelta(days=1)
        try:
            if not is_trading_day(day):
                continue
        except Exception:
            pass
        row = DetectorSignal.objects.filter(
            target_kind=target_kind,
            target_code=target_code,
            trade_date=day,
            direction=direction,
        ).first()
        if row is None:
            break
        cycles += 1
    return cycles


def _severity(score, cycles, vote_count):
    """分级：高=多维度共振且已维持；中=维持或共振一项突出；低=刚观察。"""
    if cycles >= 2 and vote_count >= 3 and score >= SEVERITY_RULES['high_score']:
        return DetectorSignal.SEVERITY_HIGH
    if cycles >= 2 or score >= SEVERITY_RULES['medium_score']:
        return DetectorSignal.SEVERITY_MEDIUM
    return DetectorSignal.SEVERITY_LOW


def evaluate_target(ctx, configs):
    """对一个已通过平静判定的对象跑全部适用维度并投票。

    返回 (signal_data | None, dim_results)：
    - signal_data 为 None 表示当日不构成信号（票数不足 / 有反向票），
      dim_results 仍然完整返回（热力图与详情页要用）；
    - signal_data.trade_date 用 ctx['as_of']（数据自身日期）：上游晚到时
      信号跟着数据日期走，不冒充扫描当天。
    """
    dim_results = {}
    up_votes, down_votes = [], []

    for cfg in configs:
        # 停用维度（DB is_enabled=False，或注册表未实现）既不评估也不投票（ALG-07）
        if not cfg.get('enabled', True) or cfg.get('evaluate') is None:
            continue
        if ctx['kind'] not in cfg['targets']:
            continue
        try:
            res = cfg['evaluate'](ctx, cfg['params'])
        except Exception as e:  # noqa: BLE001  单维度异常不拖垮整个对象
            res = {
                'available': False, 'value': None, 'direction': None,
                'note': f'维度计算异常: {e}', 'series': [],
            }
        res['name'] = cfg['name']
        res['weight'] = cfg['weight']
        dim_results[cfg['key']] = res
        if res['direction'] == 'up':
            up_votes.append((cfg['key'], res))
        elif res['direction'] == 'down':
            down_votes.append((cfg['key'], res))

    direction = None
    votes = []
    if len(up_votes) >= VOTE_MIN_DIMS and not down_votes:
        direction, votes = DetectorSignal.DIRECTION_UP, up_votes
    elif len(down_votes) >= VOTE_MIN_DIMS and not up_votes:
        direction, votes = DetectorSignal.DIRECTION_DOWN, down_votes

    if direction is None:
        return None, dim_results

    # 得分 = 同侧维度 |value| 的加权平均：衡量「共振强度」而非简单票数
    total_w = sum(res['weight'] for _k, res in votes)
    score = round(
        sum(abs(res['value'] or 0.0) * res['weight'] for _k, res in votes) / total_w, 1
    ) if total_w else 0.0

    cycles = _count_cycles(ctx['kind'], ctx['code'], direction, _as_date(ctx['as_of'])) + 1
    signal_data = {
        'target_kind': ctx['kind'],
        'target_code': ctx['code'],
        'target_name': ctx.get('name') or ctx['code'],
        'trade_date': ctx['as_of'],
        'direction': direction,
        'cycles': cycles,
        'score': score,
        'severity': _severity(score, cycles, len(votes)),
        'status': (
            DetectorSignal.STATUS_ACTIVE if cycles >= 2
            else DetectorSignal.STATUS_WATCHING
        ),
    }
    return signal_data, dim_results


def dimensions_payload(dim_results):
    """落库用的维度明细（去掉 series 大字段，详情页需要时重算）。"""
    return {
        key: {
            'name': res.get('name'),
            'value': res.get('value'),
            'direction': res.get('direction'),
            'weight': res.get('weight'),
            'available': res.get('available'),
            'note': res.get('note', ''),
        }
        for key, res in dim_results.items()
    }


def upsert_signal(signal_data, dim_results):
    """同 (kind, code, trade_date) 幂等覆盖；返回 (signal, created)。"""
    return DetectorSignal.objects.update_or_create(
        target_kind=signal_data['target_kind'],
        target_code=signal_data['target_code'],
        trade_date=signal_data['trade_date'],
        defaults={
            'target_name': signal_data['target_name'],
            'direction': signal_data['direction'],
            'cycles': signal_data['cycles'],
            'score': signal_data['score'],
            'severity': signal_data['severity'],
            'status': signal_data['status'],
            'dimensions': dimensions_payload(dim_results),
        },
    )
