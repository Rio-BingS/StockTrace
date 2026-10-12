"""信号判定：维度分组加权投票 → 方向/得分/周期 → 严重程度分级 → 幂等落库。

判定规则（#4 投票去相关，01_code_audit §2.7 / §8 #4）：
- 维度先按信息源分组（注册表条目的 'group' / 'role'）：
  - 方向组：价格（vol_compress + ma_relation，组内取强）、资金（板块净额）、
    杠杆（融资余额）——只有方向组决定 up/down；
  - 强度组：量能（vol_ratio_slope、turnover_pct，组内取强）——两者来自同一条
    成交量序列，只在「活跃度抬升」（direction='up'）时作为确认/加分，
    不决定方向、缩量也不能否决（审查发现缩量票让个股信号几乎全向下）；
  - 注册表未声明分组的维度各自成组、按方向组处理（兼容新增维度与旧口径）。
- 组票权重 = 组内代表维度（同侧 weight×|value| 最大者）的权重，即
  DetectorDimConfig.weight——权重从此参与「是否出信号」，不再只进得分平均；
- 出信号：同侧组票数（方向组 + 强度确认）≥ VOTE_MIN_DIMS、同侧加权票
  ≥ VOTE_MIN_WEIGHT、且至少一个方向组；对侧方向组加权票 < 同侧 × OPPOSE_RATIO
  （旧口径「对侧 0 票」让任何一个噪声维度都能否决）；
- 连续维持：周期数 = 1 + max(此前连续交易日的同向信号行数, 此前连续交易日
  方向组同向的天数)。后者用 ctx 截断到当日重算维度（只用 ≤ 当日的数据），
  不再要求那些天也平静、也出信号（#2：旧口径下维持中样本几乎被平静闸门耗尽）；
- 幅度温和：由各维度在评估时的温和带保证，这里不再重复判断。

与自动调参的接口（review-ui 方向）：
- 调参对象 = DetectorDimConfig.weight（边界 [0.5, 2]）：它经 scan.load_configs →
  cfg['weight'] 进入 vote()，作为该维度当组代表时的组票权重；
- dimensions_payload 为每个维度落 'group' / 'role' / 'counted'：counted=True
  表示该维度是本次信号中计票的组代表。归因请只算 counted 的维度，强度组的
  'up' 是「活跃度抬升」不是价格方向，不能按 direction==信号方向 归因；
- vote(dim_results) 是纯函数，回测/离线校准可直接调用。

修饰维度（det-v2，vote_enabled=False 的维度在评估结果里可带）：
- 'veto': {'direction', 'reason'}——投票出了同向信号时否决它（不落库，scan 记进
  DetectorRun.vetoed）；
- 'escalate': {'direction', 'reason'}——同向信号严重度升一档（高为顶），不加票；
- 'annotation': {...}——原样写进信号附注 annotations[维度 key]，只展示。
修饰只作用于已经出了的信号，不会凭空产生信号，也不改变方向与周期。
"""

from __future__ import annotations

import logging
from datetime import date as _date, timedelta

from ..models import DetectorSignal
from . import registry, trend
from .constants import (
    ACTIVE_MIN_CYCLES,
    CYCLE_LOOKBACK as _CYCLE_LOOKBACK,
    HIGH_SEVERITY_MIN_VOTES,
    OPPOSE_RATIO,
    ROLE_DIRECTION,
    ROLE_STRENGTH,
    SEVERITY_RULES,
    VOTE_MIN_DIMS,
    VOTE_MIN_WEIGHT,
)

logger = logging.getLogger(__name__)


def _as_date(value):
    """ctx['as_of'] 是 ISO 字符串（序列里带出来的），周期回看需要 date 运算。"""
    return _date.fromisoformat(value) if isinstance(value, str) else value


def _previous_trading_days(before_date, n, calendar=None):
    """before_date 之前（不含）的最近 n 个交易日，降序；日历给不出结论时返回 None。

    is_trading_day 是三态：None（未知）或抛异常都视为「日历不可用」——旧实现在
    这种情况下把自然日当交易日推进（ALG-15），节假日会被当成「当天没信号」而断链。

    calendar：可注入的交易日历（任何带三态 is_trading_day(day) 方法的对象），
    离线回测用本地日线日期构造，不触网；缺省走 stocks.trading_calendar。
    """
    if calendar is not None:
        is_trading_day = calendar.is_trading_day
    else:
        from ..trading_calendar import is_trading_day

    days = []
    day = before_date
    for _ in range(n * 3 + 15):
        day -= timedelta(days=1)
        try:
            flag = is_trading_day(day)
        except Exception:
            # is_trading_day 已吞掉上游异常，走到这里是意外错误：按日历未知处理并留痕
            logger.warning("周期回看的交易日判定失败，按日历未知处理", exc_info=True)
            return None
        if flag is None:
            return None
        if flag:
            days.append(day)
            if len(days) == n:
                return days
    return None


def _count_cycles(target_kind, target_code, direction, before_date, calendar=None):
    """信号日之前连续多少个交易日存在同方向信号行（周期维持计数）。

    为什么按信号行而非「微变状态」计数：状态没有被持久化，信号行本身就是
    每日状态快照；中间某天扫描没跑（宕机/上游全挂）周期自然中断——维持
    本来就要求逐日确认，中断后重新累计是符合语义的保守行为。

    一次查询取回看窗口内该对象同方向的信号日期，再与交易日序列逐日比对
    （PERF-08：旧实现逐日单查，每个对象最多 20 次查询）。交易日历不可用时
    返回 None（未知），不再按自然日推进（ALG-15）。calendar 见 _previous_trading_days。
    """
    days = _previous_trading_days(before_date, _CYCLE_LOOKBACK, calendar=calendar)
    if days is None:
        return None
    present = set(
        DetectorSignal.objects.filter(
            target_kind=target_kind,
            target_code=target_code,
            direction=direction,
            trade_date__gte=days[-1],
            trade_date__lt=before_date,
        ).values_list('trade_date', flat=True)
    )
    cycles = 0
    for day in days:
        if day not in present:
            break
        cycles += 1
    return cycles


def _severity(score, cycles, vote_count, groups_available=None):
    """分级：高=多组共振且已维持；中=维持或共振一项突出；低=刚观察。

    groups_available：该对象可用的信息组数。分组后个股/板块最多只有 2 组，
    固定「≥3 票」会让高等级永远不可达，因此取 min(HIGH_SEVERITY_MIN_VOTES,
    可用组数)，即「全部可用信息组同向」也算多组共振。
    """
    need = HIGH_SEVERITY_MIN_VOTES
    if groups_available is not None:
        need = max(VOTE_MIN_DIMS, min(need, groups_available))
    if (cycles >= ACTIVE_MIN_CYCLES and vote_count >= need
            and score >= SEVERITY_RULES['high_score']):
        return DetectorSignal.SEVERITY_HIGH
    if cycles >= ACTIVE_MIN_CYCLES or score >= SEVERITY_RULES['medium_score']:
        return DetectorSignal.SEVERITY_MEDIUM
    return DetectorSignal.SEVERITY_LOW


_SEVERITY_ORDER = (DetectorSignal.SEVERITY_LOW, DetectorSignal.SEVERITY_MEDIUM, DetectorSignal.SEVERITY_HIGH)


def escalate_severity(severity):
    """严重度升一档：低→中→高，高保持不变。"""
    i = _SEVERITY_ORDER.index(severity) if severity in _SEVERITY_ORDER else 0
    return _SEVERITY_ORDER[min(i + 1, len(_SEVERITY_ORDER) - 1)]


def apply_modifiers(signal_data, dim_results):
    """把修饰维度的否决/升级/附注作用到已出的信号上（纯函数，原地改 signal_data）。

    返回否决记录 dict（信号被否决）或 None。附注无论是否命中都写进
    signal_data['annotations']，让信号详情能看到分位等依据。
    """
    direction = signal_data['direction']
    annotations = dict(signal_data.get('annotations') or {})
    vetoed = None
    for key, res in dim_results.items():
        if not res.get('available'):
            continue
        note = dict(res.get('annotation') or {})
        veto = res.get('veto') or {}
        esc = res.get('escalate') or {}
        if veto.get('direction') == direction and vetoed is None:
            vetoed = {'dim': key, 'reason': veto.get('reason', '')}
            note['vetoed'] = True
            note['veto_reason'] = veto.get('reason', '')
        if esc.get('direction') == direction:
            before = signal_data['severity']
            signal_data['severity'] = escalate_severity(before)
            note['escalated'] = True
            note['escalate_reason'] = esc.get('reason', '')
            note['severity_before'] = before
        if note:
            annotations[key] = note
    signal_data['annotations'] = annotations
    if vetoed is None:
        return None
    return {
        'target_kind': signal_data['target_kind'],
        'target_code': signal_data['target_code'],
        'target_name': signal_data['target_name'],
        'trade_date': str(signal_data['trade_date']),
        'direction': direction,
        'score': signal_data['score'],
        'cycles': signal_data['cycles'],
        'severity': signal_data['severity'],
        'dim': vetoed['dim'],
        'reason': vetoed['reason'],
        'annotations': annotations,
    }


def dim_group(key, cfg=None):
    """维度所属 (信息组, 角色)：cfg/注册表声明优先，未声明则自成方向组。"""
    spec = registry.DIMENSION_MAP.get(key) or {}
    group = (cfg or {}).get('group') or spec.get('group') or key
    role = (cfg or {}).get('role') or spec.get('role') or ROLE_DIRECTION
    return group, role


def dim_votes(key, cfg=None):
    """维度是否参加投票：cfg/注册表的 vote_enabled，缺省 True（det-v2 只展示维度为 False）。"""
    if cfg is not None and 'vote_enabled' in cfg:
        return bool(cfg['vote_enabled'])
    spec = registry.DIMENSION_MAP.get(key) or {}
    return bool(spec.get('vote_enabled', True))


def _strength(res):
    return (res.get('weight') or 0.0) * abs(res.get('value') or 0.0)


def vote(dim_results):
    """分组加权投票（纯函数）：返回投票明细，direction 为 None 表示不出信号。

    dim_results: {key: {direction, value, weight, available, group?, role?}}。
    返回 {'direction', 'support', 'oppose', 'n_groups', 'groups_available',
          'groups': {group: {...}}, 'counted': [key...], 'reason'}。
    """
    groups = {}
    available = set()
    for key, res in dim_results.items():
        # 只展示/只作否决的维度（vote_enabled=False）不计票，也不算可用信息组
        if res.get('vote_enabled') is False:
            continue
        group, role = res.get('group'), res.get('role')
        if not group or not role:
            group, role = dim_group(key)
        if res.get('available'):
            available.add(group)
        if res.get('direction') not in ('up', 'down'):
            continue
        g = groups.setdefault(group, {'role': role, 'members': []})
        g['members'].append((key, res))

    summary = {}
    support = {'up': 0.0, 'down': 0.0}
    n_dir = {'up': 0, 'down': 0}
    reps = {'up': [], 'down': []}
    confirm = None  # 强度组确认：(group, key, res)
    for group, g in groups.items():
        members = g['members']
        if g['role'] == ROLE_STRENGTH:
            # 只有「活跃度抬升」算确认；缩量/降温不计票也不否决
            ups = [(k, r) for k, r in members if r['direction'] == 'up']
            if not ups:
                summary[group] = {'role': ROLE_STRENGTH, 'direction': None}
                continue
            key, res = max(ups, key=lambda kr: _strength(kr[1]))
            summary[group] = {'role': ROLE_STRENGTH, 'direction': 'up', 'rep': key,
                              'weight': res.get('weight') or 0.0}
            if confirm is None or _strength(res) > _strength(confirm[2]):
                confirm = (group, key, res)
            continue
        # 方向组：组内按 weight×|value| 有符号求和定方向（组内分歧互相抵消），
        # 代表维度取同侧最强者
        net = sum(_strength(r) * (1 if r['direction'] == 'up' else -1) for _k, r in members)
        if net == 0:
            summary[group] = {'role': ROLE_DIRECTION, 'direction': None}
            continue
        side = 'up' if net > 0 else 'down'
        key, res = max(((k, r) for k, r in members if r['direction'] == side),
                       key=lambda kr: _strength(kr[1]))
        weight = res.get('weight') or 0.0
        summary[group] = {'role': ROLE_DIRECTION, 'direction': side, 'rep': key, 'weight': weight}
        support[side] += weight
        n_dir[side] += 1
        reps[side].append((key, res))

    out = {
        'direction': None, 'support': 0.0, 'oppose': 0.0, 'n_groups': 0,
        'groups_available': len(available), 'groups': summary, 'counted': [], 'reason': '',
        'dir_support': {k: round(v, 3) for k, v in support.items()},
    }
    if support['up'] == support['down']:
        out['reason'] = '方向组无票或两侧持平'
        return out
    side = 'up' if support['up'] > support['down'] else 'down'
    other = 'down' if side == 'up' else 'up'
    counted = list(reps[side])
    total = support[side]
    n_groups = n_dir[side]
    if confirm is not None:
        counted.append((confirm[1], confirm[2]))
        total += confirm[2].get('weight') or 0.0
        n_groups += 1
    out.update({
        'support': round(total, 3), 'oppose': round(support[other], 3), 'n_groups': n_groups,
        'counted': [k for k, _r in counted], '_counted_res': counted, 'side': side,
    })
    if support[other] >= OPPOSE_RATIO * support[side]:
        out['reason'] = '对侧方向组加权票过多（维度分歧）'
        return out
    if n_groups < VOTE_MIN_DIMS or total < VOTE_MIN_WEIGHT:
        out['reason'] = '同侧信息组票数或加权票不足'
        return out
    out['direction'] = side
    return out


def direction_state(dim_results):
    """方向组的同向状态（不要求强度确认、不要求平静）：周期维持用。

    同侧方向组有票、且对侧方向组加权票 < 同侧 × OPPOSE_RATIO 即为该方向。
    """
    v = vote(dim_results)
    up, down = v['dir_support']['up'], v['dir_support']['down']
    if up == down:
        return None
    side, same, opp = ('up', up, down) if up > down else ('down', down, up)
    return side if opp < OPPOSE_RATIO * same else None


def _evaluate_dims(ctx, configs):
    """跑全部适用维度，返回 dim_results（单维度异常只降级自己）。"""
    dim_results = {}
    for cfg in configs:
        # 停用维度（DB is_enabled=False，或注册表未实现）既不评估也不投票（ALG-07）
        if not cfg.get('enabled', True) or cfg.get('evaluate') is None:
            continue
        if ctx['kind'] not in cfg['targets']:
            continue
        try:
            res = cfg['evaluate'](ctx, cfg['params'])
        except Exception as e:  # 边界：单维度异常不拖垮整个对象
            logger.warning("探测器维度 %s 计算异常: %s", cfg['key'], e, exc_info=True)
            res = {
                'available': False, 'value': None, 'direction': None,
                'note': f'维度计算异常: {e}', 'series': [],
            }
        group, role = dim_group(cfg['key'], cfg)
        res['vote_enabled'] = dim_votes(cfg['key'], cfg)
        res['name'] = cfg['name']
        res['weight'] = cfg['weight']
        res['params'] = dict(cfg.get('params') or {})
        res['group'], res['role'] = group, role
        dim_results[cfg['key']] = res
    return dim_results


# ctx 里按数据日存放、长度与 dates 无关的行序列：截断时按「数据日 < 截断日」过滤
_DATED_ROW_KEYS = ('margin', 'stock_margin')


def slice_ctx(ctx, end):
    """把 ctx 截断到第 end 根（含）：与 dates 等长的序列一并截断，as_of 前移。

    融资余额行（指数 margin、个股 stock_margin）按数据日 < 截断日保留（T+1 披露：
    T 日扫描时最新只有 T−1）。只用 ≤ 截断日的数据，周期回看不引入前视。
    """
    dates = ctx.get('dates') or []
    n = len(dates)
    out = dict(ctx)
    for key, val in ctx.items():
        if key in ('dates',) + _DATED_ROW_KEYS:
            continue
        if isinstance(val, list) and len(val) == n:
            out[key] = val[:end + 1]
    out['dates'] = dates[:end + 1]
    out['as_of'] = dates[end]
    for key in _DATED_ROW_KEYS:
        if isinstance(ctx.get(key), list):
            out[key] = [m for m in ctx[key] if str(m.get('date')) < str(dates[end])]
    return out


def _state_streak(ctx, configs, direction, days):
    """此前连续多少个交易日方向组与 direction 同向（ctx 截断重算）。

    days：信号日之前的交易日（降序，来自交易日历）。ctx 里缺某个交易日
    （停牌/缺快照）即断链，与按信号行计数的保守语义一致。
    """
    dates = ctx.get('dates') or []
    if not dates or not days:
        return 0
    index = {str(d): i for i, d in enumerate(dates)}
    streak = 0
    for day in days:
        i = index.get(day.isoformat() if hasattr(day, 'isoformat') else str(day))
        if i is None or i < 1:
            break
        try:
            dims = _evaluate_dims(slice_ctx(ctx, i), configs)
        except Exception:  # noqa: BLE001 边界：回看失败只让周期少算，不影响当日信号
            logger.warning("周期回看重算维度失败 %s", ctx.get('code'), exc_info=True)
            break
        if direction_state(dims) != direction:
            break
        streak += 1
    return streak


def evaluate_target(ctx, configs, cycle_counter=None, calendar=None):
    """对一个已通过平静判定的对象跑全部适用维度并分组投票。

    注入点（离线回测用，缺省行为不变）：
    - cycle_counter(target_kind, target_code, direction, before_date)：替代查库的
      _count_cycles（回测用内存信号表），返回值语义相同（None=日历不可用）；
    - calendar：透传给默认 _count_cycles 与状态回看的 _previous_trading_days。

    返回 (signal_data | None, dim_results)：
    - signal_data 为 None 表示当日不构成信号（组票不足 / 对侧分歧），
      dim_results 仍然完整返回（热力图与详情页要用）；
    - signal_data.trade_date 用 ctx['as_of']（数据自身日期）：上游晚到时
      信号跟着数据日期走，不冒充扫描当天。
    """
    dim_results = _evaluate_dims(ctx, configs)
    result = vote(dim_results)
    counted = result.pop('_counted_res', [])
    result.pop('side', None)
    for key, res in dim_results.items():
        res['counted'] = bool(result['direction']) and key in result['counted']
    direction = result['direction']
    if direction is None:
        return None, dim_results

    # 得分 = 计票组代表 |value| 的加权平均：衡量「共振强度」而非简单票数
    total_w = sum(res['weight'] for _k, res in counted)
    score = round(
        sum(abs(res['value'] or 0.0) * res['weight'] for _k, res in counted) / total_w, 1
    ) if total_w else 0.0

    as_of = _as_date(ctx['as_of'])
    # 日历注入只在给了 calendar 时透传：缺省调用与旧签名逐字一致（测试 patch 照常生效）
    cal_kwargs = {'calendar': calendar} if calendar is not None else {}
    if cycle_counter is None:
        # 运行时按模块属性查找：测试 patch signals._count_cycles 照常生效
        prev_cycles = _count_cycles(ctx['kind'], ctx['code'], direction, as_of, **cal_kwargs)
    else:
        prev_cycles = cycle_counter(ctx['kind'], ctx['code'], direction, as_of)
    streak = 0
    # ctx 没有日期序列（测试桩）时无从回看，也就不必查交易日历
    if prev_cycles is not None and ctx.get('dates'):
        # 状态回看的交易日序列同样走注入的日历（离线回测不触网）
        days = _previous_trading_days(as_of, _CYCLE_LOOKBACK, **cal_kwargs)
        streak = _state_streak(ctx, configs, direction, days) if days else 0
    # 日历不可用：周期无法确认，按 1 记（只会少算），并让 scan 记一条降级
    cycles = max(prev_cycles or 0, streak) + 1
    signal_data = {
        'target_kind': ctx['kind'],
        'target_code': ctx['code'],
        'target_name': ctx.get('name') or ctx['code'],
        'trade_date': ctx['as_of'],
        'direction': direction,
        'cycles': cycles,
        'score': score,
        'severity': _severity(score, cycles, result['n_groups'], result['groups_available']),
        'status': (
            DetectorSignal.STATUS_ACTIVE if cycles >= ACTIVE_MIN_CYCLES
            else DetectorSignal.STATUS_WATCHING
        ),
        'cycles_unknown': prev_cycles is None,
        # 扫描侧排序/截断用的强度（#11）：得分 × 同侧加权票，不落库
        'strength': round(score * result['support'], 2),
        'vote': result,
    }
    # 趋势状态（det-v2，第七轮）：信号所处的 ZigZag 波段，只用 T 日及以前，只展示
    try:
        state = trend.trend_state(ctx)
    except Exception:  # noqa: BLE001 边界：趋势状态只是展示，失败不影响信号
        logger.warning("趋势状态计算失败 %s", ctx.get('code'), exc_info=True)
        state = None
    if state:
        signal_data['annotations'] = {'trend': state}
    # 修饰维度（det-v2）：否决 → 当日不出信号，否决记录挂在 dim_results 上供 scan 留痕
    vetoed = apply_modifiers(signal_data, dim_results)
    if vetoed is not None:
        dim_results[vetoed['dim']]['vetoed_signal'] = vetoed
        return None, dim_results
    return signal_data, dim_results


def vetoed_record(dim_results):
    """evaluate_target 否决了信号时返回否决记录，否则 None（scan 写 DetectorRun.vetoed 用）。"""
    for res in dim_results.values():
        if res.get('vetoed_signal'):
            return res['vetoed_signal']
    return None


def dimensions_payload(dim_results):
    """落库用的维度明细（去掉 series 大字段，详情页需要时重算）。

    params 是信号当日生效的维度参数快照（ALG-15）：自动调参会改 min/权重，
    详情页重算曲线要用信号当时的参数，否则曲线与落库判定对不上。
    """
    return {
        key: {
            'name': res.get('name'),
            'value': res.get('value'),
            'direction': res.get('direction'),
            'weight': res.get('weight'),
            'available': res.get('available'),
            'note': res.get('note', ''),
            'params': res.get('params'),
            # 投票分组（#4）：归因只算 counted 的组代表，见模块 docstring
            'group': res.get('group'),
            'role': res.get('role'),
            'counted': bool(res.get('counted')),
            # 只展示/只作否决的维度（det-v2），前端据此不显示为「投票」
            'vote_enabled': res.get('vote_enabled', True),
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
            'annotations': signal_data.get('annotations') or {},
            # 来源（成分池扩容）：scan 写入；缺省时个股按自选、其余按指数/板块
            'source': signal_data.get('source') or (
                DetectorSignal.SOURCE_WATCHLIST if signal_data['target_kind'] == DetectorSignal.TARGET_STOCK
                else DetectorSignal.SOURCE_MARKET),
        },
    )
