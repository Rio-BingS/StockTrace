"""探测器维度计算实现：数学工具、对象序列构建与各微变维度的评估函数。

设计约定：
- ctx（对象上下文）是各维度共用的序列容器，index/stock 为真实 OHLCV，
  sector 为本站日度快照派生的 net / change_pct 序列；日期一律升序。
- 每个维度 evaluate(ctx, params) 返回统一结构：
  {available, value, direction, note, series}
  value 是归一化的「同向分值」（-100..100，符号恒与 direction 一致，up 为正），
  供热力图与加权投票共用；direction=None 表示该维度当日不投票（无微变 /
  幅度超出温和区间 / 数据不足），但 value 可能仍有展示意义。
- 所有阈值来自 params（registry 默认值 + DetectorDimConfig 持久化调整），
  代码里不出现魔法数；数据不足一律 available=False，绝不补 0。
"""

from __future__ import annotations

from ..models import DailyQuote, MarketDailySnapshot

# ============================================================
# 数学小工具（只依赖纯 Python，方便离线单测）
# ============================================================


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _std(vals):
    vals = [v for v in vals if v is not None]
    n = len(vals)
    if n < 2:
        return None
    m = sum(vals) / n
    return (sum((v - m) ** 2 for v in vals) / (n - 1)) ** 0.5


def _percentile(vals, q):
    """线性插值分位（q ∈ 0..1）；空序列返回 None。"""
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = q * (len(vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return vals[lo] * (1 - frac) + vals[hi] * frac


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _tail(vals, k):
    """取末尾 k 个值；不足或含 None 返回 None（窗口不齐就不算，不补天）。"""
    if vals is None or len(vals) < k:
        return None
    window = vals[-k:]
    if any(v is None for v in window):
        return None
    return window


def _sign_value(raw, params):
    """把原始漂移值归一化为同向分值，并按 [min, max] 温和区间给出投票方向。

    - |raw| < min：变化太弱，不构成「微变」，不投票；
    - |raw| > max：变化过猛，疑似已进入突变/趋势展开阶段，不是早期迹象，
      如实标注不投票（这是「识别早期而非追认突变」的关键闸门）；
    - value = raw/mild × 50，让 ±mild（温和带边界）对应 ±50 分。

    边界（ALG-15）：raw == 0 没有方向，不投票（旧实现 min=0 时会判成 down）；
    参数可在库里手改，mild ≤ 0 或不满足 0 ≤ min < max 视为配置错误，如实
    不投票、value 为 None（旧实现 mild=0 时热力图显示 0 却照常投票）。
    """
    mild, lo, hi = params.get('mild'), params.get('min'), params.get('max')
    if not _params_valid(mild, lo, hi):
        return None, None, '维度参数非法（需 mild > 0 且 0 ≤ min < max），不投票'
    value = _clamp(raw / mild * 50, -100, 100)
    if raw == 0 or abs(raw) < lo:
        return value, None, ''
    if abs(raw) > hi:
        return value, None, '幅度超出温和区间，疑似突变而非早期迹象'
    return value, ('up' if raw > 0 else 'down'), ''


def _params_valid(mild, lo, hi):
    """温和带参数合法性：mild > 0 且 0 ≤ min < max（数值类型）。"""
    try:
        return float(mild) > 0 and 0 <= float(lo) < float(hi)
    except (TypeError, ValueError):
        return False


def _noise_gate(raw, history, params):
    """噪声闸门（ALG-04）：|raw| 必须超过自身历史 |raw| 分布的 noise_pct 分位才投票。

    为什么需要：min 是跨品种的绝对常数，远低于单日量比/分位/均线距离的自然
    噪声——随机游走模拟下平静样本约三成会出「≥2 维同向」信号。把投票门槛
    换成「相对自己的历史是否异常」，阈值随品种自身噪声缩放。
    history 是当前点之前的 raw 序列（不含当前点，含 None 时跳过）；
    noise_pct 为 0/缺省时关闭闸门（向前兼容）。返回不投票原因，通过返回 ''。
    """
    q = params.get('noise_pct')
    if not q:
        return ''
    window = int(params.get('noise_window', 60))
    need = int(params.get('noise_min', 15))
    sample = [abs(v) for v in history if v is not None][-window:]
    if len(sample) < need:
        return f'噪声基线样本不足（{len(sample)}/{need}），不投票'
    thr = _percentile(sample, q)
    if abs(raw) <= thr:
        return f'漂移未超出自身历史 {q:.0%} 分位（噪声带 ±{thr:.3g}），不投票'
    return ''


def _gated(raw, history, params):
    """_sign_value + 噪声闸门：温和带内且超出自身噪声带才投票。"""
    value, direction, note = _sign_value(raw, params)
    if direction is not None:
        gate = _noise_gate(raw, history, params)
        if gate:
            direction, note = None, gate
    return value, direction, note


def _ols_slope(ys):
    """等间距序列的最小二乘斜率（x = 0..n-1）；含 None 或不足 2 点返回 None。

    比两端点差分稳健：单日量比的噪声只按 1/n 权重进入斜率，而不是全额。
    """
    if not ys or len(ys) < 2 or any(v is None for v in ys):
        return None
    n = len(ys)
    mx = (n - 1) / 2
    my = sum(ys) / n
    sxx = sum((i - mx) ** 2 for i in range(n))
    return sum((i - mx) * (y - my) for i, y in enumerate(ys)) / sxx


def _unavailable(note):
    return {'available': False, 'value': None, 'direction': None, 'note': note, 'series': []}


def _result(value, direction, note, series):
    return {
        'available': True,
        'value': round(value, 2) if value is not None else None,
        'direction': direction,
        'note': note,
        'series': series,
    }


def _series_points(dates, values, count=20):
    """末尾 count 个 (date, value) 点，供详情页曲线；None 保留（断点显示）。"""
    out = []
    for d, v in list(zip(dates, values))[-count:]:
        out.append({'date': d, 'value': round(v, 3) if v is not None else None})
    return out


# ============================================================
# 对象序列构建（scan 与 signal 详情共用）
# ============================================================

# index/stock 目标的最低历史门槛：低于它连平静判定的滚动窗口都凑不齐，
# 整个对象跳过（如实在 run.degraded 里记录原因）。
MIN_PRICE_BARS = 30
# sector 目标（快照派生序列）的最低有效天数：覆盖 ma20/波动窗口的底线。
MIN_SECTOR_POINTS = 12


def build_index_context(code, name):
    """指数上下文：新浪日线 OHLCV（同一上游接口，见 market.indices）+ 融资余额快照序列。"""
    from ..market import indices

    data = indices.fetch_index_daily_series(code, days=70)
    if not data.get('available'):
        return None, data.get('message') or '指数日线不可用'
    items = data['items']
    if len(items) < MIN_PRICE_BARS:
        return None, f'指数日线仅 {len(items)} 根，历史不足'

    ctx = _price_ctx('index', code, name, items)
    # 融资余额序列：本站日度快照积累（T+1 披露），给 margin_north 维度用；
    # 积累不足时维度自行 available=False，不影响其他维度。
    rows = (
        MarketDailySnapshot.objects.filter(kind=MarketDailySnapshot.KIND_MARGIN)
        .order_by('-trade_date')
        .values_list('trade_date', 'payload')[:40]
    )
    margin = []
    for trade_date, payload in reversed(list(rows)):
        for row in payload or []:
            if row.get('total') is not None:
                margin.append({'date': trade_date.isoformat(), 'total': row['total']})
                break
    ctx['margin'] = margin
    return ctx, ''


def build_stock_context(stock):
    """自选股上下文：DailyQuote（本站已拉取的日线），零新增上游请求。

    只用已收盘的日线（DAT-13）：盘中拉到的当日 bar 量价不完整，混进序列会
    让振幅/量比/均线距离按半天数据投票。
    """
    rows = list(
        DailyQuote.objects.filter(stock=stock, is_final=True)
        .order_by('-trade_date')
        .values_list(
            'trade_date', 'open_price', 'high_price', 'low_price',
            'close_price', 'volume',
        )[:70]
    )[::-1]
    if len(rows) < MIN_PRICE_BARS:
        return None, f'日线仅 {len(rows)} 根，历史不足'
    items = [
        {
            'date': r[0].isoformat(),
            'open': float(r[1]) if r[1] is not None else None,
            'high': float(r[2]) if r[2] is not None else None,
            'low': float(r[3]) if r[3] is not None else None,
            'close': float(r[4]) if r[4] is not None else None,
            'volume': float(r[5]) if r[5] is not None else None,
        }
        for r in rows
    ]
    return _price_ctx('stock', stock.code, stock.name, items), ''


def _price_ctx(kind, code, name, items):
    """把 OHLCV 行列表转成 ctx 序列（振幅在维度里按 prev_close 现算）。"""
    return {
        'kind': kind,
        'code': code,
        'name': name,
        'dates': [it['date'] for it in items],
        'open': [it.get('open') for it in items],
        'high': [it.get('high') for it in items],
        'low': [it.get('low') for it in items],
        'close': [it.get('close') for it in items],
        'volume': [it.get('volume') for it in items],
        'as_of': items[-1]['date'],
    }


def build_sector_context(kind, name, lookback_days=45):
    """板块上下文：本站日度快照透视出的逐日 net / change_pct 序列。

    「某板块某天缺行就 None」：回填可能只补到部分板块，绝不能拿旧值或 0
    补天（与快照模块「窗口齐全才算数」同一纪律）；各维度在取窗时遇 None
    自行如实不可用。
    """
    kind_db = (
        MarketDailySnapshot.KIND_INDUSTRY_FF
        if kind == 'industry'
        else MarketDailySnapshot.KIND_CONCEPT_FF
    )
    rows = (
        MarketDailySnapshot.objects.filter(kind=kind_db)
        .order_by('-trade_date')
        .values_list('trade_date', 'payload')[:lookback_days]
    )
    rows = list(rows)[::-1]  # 升序
    dates, nets, pcts = [], [], []
    valid = 0
    for trade_date, payload in rows:
        row = next(
            (r for r in (payload or []) if r.get('name') == name), None
        )
        dates.append(trade_date.isoformat())
        net = row.get('net') if row else None
        pct = row.get('change_pct') if row else None
        nets.append(net)
        pcts.append(pct)
        if net is not None:
            valid += 1
    if valid < MIN_SECTOR_POINTS or not dates:
        return None, f'快照窗口内仅 {valid} 个有效交易日，历史不足'

    as_of = dates[-1]
    # as_of 取该板块自己最后一个有值的日子：板块缺最近一天时，信号日期
    # 跟随其真实数据日期，不冒充当天。
    for i in range(len(dates) - 1, -1, -1):
        if nets[i] is not None:
            as_of = dates[i]
            break
    ctx = {
        'kind': kind,
        'code': name,
        'name': name,
        'dates': dates,
        'net': nets,
        'change_pct': pcts,
        'as_of': as_of,
    }
    return ctx, ''


# ============================================================
# 各维度评估函数（注册表 registry.DIMENSIONS 引用）
# ============================================================


def eval_vol_ratio_slope(ctx, params):
    """量比斜率（指数/个股）：RVOL 近几日的最小二乘斜率（日均漂移）。

    为什么看斜率而不是当日 RVOL：平静判定已要求当日 RVOL ∈ [0.7, 1.5]，
    「平静中的微变」体现在量比的缓慢趋势上——资金悄悄回流的最早形态
    就是量比温和抬升，而不是放量本身。
    噪声校准（ALG-04）：斜率用 slope_days+1 个点的 OLS 而不是两端点差分，
    且 |斜率| 须超出自身历史斜率的 noise_pct 分位才投票。
    """
    volumes = ctx.get('volume')
    window, slope_days = params['window'], params['slope_days']
    need = window + slope_days + 1
    if not volumes or len(volumes) < need:
        return _unavailable('成交量历史不足')

    # 逐点 RVOL：基期窗口要求齐全（窗口不齐不算）
    full = [None] * len(volumes)
    for i in range(window, len(volumes)):
        win = volumes[i - window:i]
        if volumes[i] is None or any(v is None for v in win):
            continue
        base = _mean(win)
        full[i] = volumes[i] / base if base else None
    pts = slope_days + 1
    slopes = [
        _ols_slope(full[i + 1 - pts:i + 1]) if i + 1 >= pts else None
        for i in range(len(full))
    ]
    raw = slopes[-1]
    if raw is None:
        return _unavailable('量比窗口存在缺失数据')

    value, direction, note = _gated(raw, slopes[:-1], params)
    # 详情曲线：全序列 RVOL（滚动窗口逐点）
    shown = [round(v, 3) if v is not None else None for v in full[window:]]
    return _result(
        value, direction, note,
        _series_points(ctx['dates'][window:], shown),
    )


def _rolling_mean_series(vals, k, strict=False):
    """滚动均值；strict=True 时窗口内任一值为 None 该点即为 None（窗口不齐不算）。"""
    out = []
    for i in range(len(vals)):
        if i + 1 < k:
            out.append(None)
            continue
        window = vals[i + 1 - k:i + 1]
        if strict and any(v is None for v in window):
            out.append(None)
            continue
        # 不在计算层舍入（DAT-11/DET-09）：价格已是 3 位小数，round(m, 2) 会让
        # 低价 ETF 的均线距离产生系统误差；展示层（_series_points/_result）再舍入
        out.append(_mean(window))
    return out


def eval_big_order_net(ctx, params):
    """大单净流入（指数/板块）：近 6 日均净额对比前 6 日的相对漂移。

    指数用本站大盘快照的「超大单+大单」合计，板块用快照的主力净额——
    都是收盘后落库的真实上游数据。为什么窗口取偶数长度（6 日）：
    资金流在平静期常出现逐日正负交替，奇数窗口（如 5 日）的均值会残留
    ±1 天的相位噪声、制造假漂移；偶数窗口把交替完全抵消，趋势才浮现。
    分母取窗口内平均绝对净额，把大盘/板块的量级差异归一，阈值才能跨对象通用。
    """
    nets = ctx.get('net')
    window = params['window']
    mean_days = params['mean_days']
    need = window + mean_days * 2
    if not nets or len(nets) < need:
        return _unavailable('净额序列历史不足')
    if any(v is None for v in nets[-need:]):
        return _unavailable('净额窗口存在缺失快照')

    m_avg = _rolling_mean_series(nets, mean_days, strict=True)
    scale = _mean([abs(v) for v in nets[-window:]]) or 0.0
    if scale <= 0:
        return _unavailable('净额量级异常')

    def raw_at(i):
        if i - mean_days < 0 or i + 1 < window:
            return None
        win = nets[i + 1 - window:i + 1]
        if any(v is None for v in win) or m_avg[i] is None or m_avg[i - mean_days] is None:
            return None
        sc = _mean([abs(v) for v in win])
        return (m_avg[i] - m_avg[i - mean_days]) / sc if sc else None

    raws = [raw_at(i) for i in range(len(nets))]
    raw = raws[-1]
    if raw is None:
        return _unavailable('净额窗口存在缺失快照')
    value, direction, note = _gated(raw, raws[:-1], params)
    return _result(
        value, direction, note,
        _series_points(ctx['dates'], m_avg),
    )


def _chain_return(pcts, k):
    """涨跌幅序列末尾 k 日的链式累计收益（%）；含 None 返回 None。"""
    tail = _tail(pcts, k)
    if tail is None:
        return None
    prod = 1.0
    for p in tail:
        prod *= 1 + p / 100
    return (prod - 1) * 100


def _std_strict(vals):
    """滚动窗口标准差（严格版）：窗口内任一值为 None 就返回 None。

    为什么不用 _std（它静默跳过 None）：波动率窗口缺一天还硬算，等于拿
    缩水样本冒充完整窗口，违反「窗口不齐就算数」的纪律。
    """
    if any(v is None for v in vals):
        return None
    return _std(vals)


def _return_series(ctx):
    """逐日涨跌幅（%）：板块直接用快照 change_pct，指数/个股由收盘价现算。"""
    if ctx.get('change_pct') is not None:
        return list(ctx['change_pct'])
    closes = ctx.get('close')
    if not closes:
        return None
    out = [None]
    for i in range(1, len(closes)):
        c, pc = closes[i], closes[i - 1]
        out.append((c / pc - 1) * 100 if c is not None and pc else None)
    return out


def eval_vol_compress(ctx, params):
    """波动率压缩（全部对象）：近 k 日涨跌幅标准差处于自身历史低分位 + 压缩期内显著漂移。

    波动率压缩本身不指方向；投票方向取压缩区间内的价格漂移方向——
    「缩而价稳且缓慢抬升」是吸筹蓄势的典型形态，反之亦然。未压缩时
    如实显示「波动未压缩」且不投票。

    口径（ALG-04/DET-18）：
    - 旧实现只比较两个重叠 5 日窗口的单点（widths[-1] < widths[-4]），噪声下
      近一半时间成立；现改为当前宽度 ≤ 自身历史宽度的 compress_pct 分位；
    - 宽度统一用「逐日涨跌幅」的标准差（板块与指数/个股同口径），不再用振幅——
      平静闸门已经要求振幅低位，两边都用振幅会让压缩条件与闸门高度相关；
    - 漂移须相对窗口内自身波动显著：t = 均值 / (标准差/√k) 的绝对值 ≥ drift_z，
      否则视为压缩期内的随机游走，不投票。
    """
    k = params['window']
    rets = _return_series(ctx)
    if rets is None:
        return _unavailable('缺少价格/涨跌幅数据')
    if len(rets) < k + 5:
        return _unavailable('涨跌幅序列不足')
    widths = [
        _std_strict(rets[i - k + 1:i + 1]) if i + 1 >= k else None
        for i in range(len(rets))
    ]
    drift_series = [_chain_return(rets[: i + 1], k) for i in range(len(rets))]
    drift = drift_series[-1]
    cur = _tail(rets, k)
    if widths[-1] is None or drift is None or cur is None:
        return _unavailable('波动窗口存在缺失数据')

    # 详情曲线：k 日滚动漂移（带方向的量），压缩状态用 note 表达
    series = _series_points(ctx['dates'], drift_series)

    base = [w for w in widths[:-1] if w is not None][-int(params.get('noise_window', 60)):]
    need = int(params.get('noise_min', 15))
    if len(base) < need:
        return _unavailable(f'波动基线样本不足（{len(base)}/{need}）')
    thr = _percentile(base, params.get('compress_pct', 0.25))
    if widths[-1] > thr:
        return _result(0.0, None, '波动未压缩，不构成蓄势形态', series)

    value, direction, note = _sign_value(drift, params)
    if direction is not None:
        mean_r = sum(cur) / k
        w = widths[-1]
        tstat = float('inf') if w == 0 else abs(mean_r) / (w / k ** 0.5)
        if tstat < params.get('drift_z', 0.0):
            direction, note = None, f'压缩期内漂移不显著（t={tstat:.2f}），不投票'
    # 温和带：|漂移| 在 (min, max] 内才投票；压缩但漂移过小=完全静止
    if direction is None and not note:
        note = '压缩期内无有效漂移'
    return _result(value, direction, note, series)


def eval_turnover_pct(ctx, params):
    """换手率分位（指数/个股）：成交量自身 60 日分位的 5 日变化。

    口径说明（已在文档中披露）：个股窗口内流通股本基本不变，换手率 =
    量/股本 与成交量单调等价，分位排序完全一致，故用成交量分位实现，
    零新增上游请求；指数无股本概念，为市场活跃度代理。
    方向语义：分位从低位缓慢抬升（仍不热）= 关注度悄然回升；从高位回落
    （仍不死）= 温和降温。分位本身过高/过低时不动（已是活跃/冰点状态）。
    """
    volumes = ctx.get('volume')
    window, delta_days = params['window'], params['delta_days']
    if not volumes or len(volumes) < delta_days + 6:
        return _unavailable('成交量历史不足')

    valid = [v for v in volumes if v is not None]
    if len(valid) < MIN_PRICE_BARS:
        return _unavailable('有效成交量样本不足')

    def pct_at(i):
        end = i + 1
        start = max(0, end - window)
        sample = [v for v in volumes[start:end] if v is not None]
        v = volumes[i]
        if v is None or len(sample) < MIN_PRICE_BARS:
            return None
        return sum(1 for s in sample if s < v) / len(sample) * 100

    pct_series = [pct_at(i) for i in range(len(volumes))]
    if pct_series[-1] is None or pct_series[-1 - delta_days] is None:
        return _unavailable('分位窗口存在缺失数据')

    raws = [
        pct_series[i] - pct_series[i - delta_days]
        if i >= delta_days and pct_series[i] is not None and pct_series[i - delta_days] is not None
        else None
        for i in range(len(pct_series))
    ]
    raw = raws[-1]
    last = pct_series[-1]
    value, direction, note = _gated(raw, raws[:-1], params)
    # 位置闸门：分位 >70 时再抬升就是「热」而非「微变」；<30 时再降温即冰点
    if direction == 'up' and last > params['pos_cap']:
        direction, note = None, f'分位已达 {last:.0f}，超出温和区间'
    elif direction == 'down' and last < params['pos_floor']:
        direction, note = None, f'分位已低至 {last:.0f}，超出温和区间'
    return _result(
        value, direction, note,
        _series_points(ctx['dates'], pct_series),
    )


def eval_margin_north(ctx, params):
    """融资余额渐进变化（指数维度）。

    口径说明：北向资金自 2024-08 起停止逐日净买额披露（项目已如实转
    null），本维度仅融资余额口径；数据来自交易所官方披露、经本站日度
    快照积累，窗口不足 6 行时如实不可用（上线初期约需一周积累）。
    """
    margin = ctx.get('margin') or []
    if len(margin) < 6:
        return _unavailable('融资余额快照积累不足（T+1 披露，逐日积累中）')
    totals = [m['total'] for m in margin]
    m3 = _rolling_mean_series(totals, 3)
    if m3[-1] is None or m3[-4] is None or not m3[-4]:
        return _unavailable('融资余额均值窗口不齐')
    raws = [
        (m3[i] / m3[i - 3] - 1) * 100 if i >= 3 and m3[i] is not None and m3[i - 3] else None
        for i in range(len(m3))
    ]
    raw = raws[-1]
    value, direction, note = _gated(raw, raws[:-1], params)
    if direction is not None:
        note = '仅融资余额口径（北向已停止逐日披露）'
    return _result(
        value, direction, note,
        _series_points([m['date'] for m in margin], m3),
    )


def _sector_price_series(ctx):
    """板块伪价格序列：由真实日涨跌幅链式合成（基期 100）。

    这不是伪造价格——是对真实涨跌幅的可加变换（与计算 MA 同类），用于
    让板块也有「价格与均线关系」维度；基期任意，只影响水平不影响形态。

    缺口处理（ALG-06）：某天涨跌幅缺失时该点输出 None，level 保持上一个
    值继续链乘，而不是把 level 清成 None（旧实现下一天会 None * float
    抛异常，整个 45 日窗口内该维度都不可用）。缺口前后的水平因少乘一天
    而不可比，所以 eval_ma_relation 要求均线窗口内无缺失点——窗口全部
    落在缺口之后时，链条连续，结果与无缺口时一致。
    """
    level, out = 100.0, []
    for p in ctx.get('change_pct') or []:
        if p is None:
            out.append(None)
            continue
        level = level * (1 + p / 100)
        out.append(level)
    return out


def eval_ma_relation(ctx, params):
    """价格与均线关系（全部对象）：20 日均线距离的 3 日漂移。

    为什么用「距离的漂移」而不是「价格在上/在下」：平静期价格贴近均线，
    距离开始单侧缓慢扩大是趋势萌芽的最早形态；而距离已拉得很远说明趋势
    展开（追认而非早期），如实不投票。
    """
    ma_days, slope_days = params['ma'], params['slope_days']
    if ctx.get('change_pct') is not None:
        closes = _sector_price_series(ctx)
        dates = ctx['dates']
    else:
        closes, dates = ctx.get('close'), ctx['dates']
    if not closes or len(closes) < ma_days + slope_days + 1:
        return _unavailable('收盘序列不足（均线窗口不齐）')
    if any(v is None for v in closes[-(ma_days + slope_days + 1):]):
        return _unavailable('均线窗口存在缺失数据')

    # 严格窗口：板块伪价格缺口前后水平不可比，跨缺口的均线不能算（ALG-06）
    ma = _rolling_mean_series(closes, ma_days, strict=True)
    dist = [
        (closes[i] / ma[i] - 1) * 100 if ma[i] and closes[i] is not None else None
        for i in range(len(closes))
    ]
    if dist[-1] is None or dist[-1 - slope_days] is None:
        return _unavailable('均线距离序列不齐')
    raws = [
        dist[i] - dist[i - slope_days]
        if i >= slope_days and dist[i] is not None and dist[i - slope_days] is not None
        else None
        for i in range(len(dist))
    ]
    raw = raws[-1]
    value, direction, note = _gated(raw, raws[:-1], params)
    if abs(dist[-1]) > params['max_dist'] and direction is not None:
        direction, note = None, f'距均线 {dist[-1]:.1f}%，趋势已展开，非早期迹象'
    return _result(
        value, direction, note,
        _series_points(dates, dist),
    )
