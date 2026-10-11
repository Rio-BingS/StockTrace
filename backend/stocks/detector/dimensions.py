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

from datetime import date as _date, timedelta

from ..models import DailyQuote, MarketDailySnapshot
# 阈值数值集中在 constants.py（ARC-06），这里保留同名引用，dimensions.X 旧路径照常可用
from .constants import (
    AXIS_CALENDAR_BUFFER,
    AXIS_CALENDAR_MULTIPLIER,
    COMPRESS_PCT_DEFAULT,
    CONTEXT_BARS,
    MARGIN_CONTEXT_DAYS,
    MARGIN_VALUE_KEY,
    MARGIN_VOTE_CODES,
    MIN_PRICE_BARS,
    MIN_SECTOR_POINTS,
    NOISE_DEFAULTS,
    SECTOR_CONTEXT_DAYS,
    SECTOR_LOOKBACK_DAYS,  # noqa: F401  旧路径 dimensions.SECTOR_LOOKBACK_DAYS 保留
    SPLIT_RATIO_DEFAULT,
)

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
    window = int(params.get('noise_window', NOISE_DEFAULTS['noise_window']))
    need = int(params.get('noise_min', NOISE_DEFAULTS['noise_min']))
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

# 最低历史门槛 MIN_PRICE_BARS / MIN_SECTOR_POINTS 与融资余额投票范围
# MARGIN_VOTE_CODES（ALG-09）见 constants.py。


def trading_day_axis(end_date, n, known_dates=()):
    """截止 end_date（含）往前 n 个交易日的日期轴，升序（#8）。

    为什么不再用「最近 N 行」：整天缺快照（调度宕机、上游挂了）时，按行取数
    会把缺口前后当成相邻日，窗口语义与漂移都被无声压缩。日期轴由交易日历
    生成，缺失日由调用方填 None，各维度取窗时如实不可用。
    交易日 = 日历明确判定为交易日，或本站已有该日数据（数据本身证明开市，
    日历缺口时不丢真实数据）；日历完全不可用时退化为「已有数据的日期」，
    与旧的按行取数等价，不会更差。往回最多走 n × 倍数 + 缓冲 个自然日。
    """
    from .. import trading_calendar

    known = set(known_dates)
    axis, day = [], end_date
    for _ in range(n * AXIS_CALENDAR_MULTIPLIER + AXIS_CALENDAR_BUFFER):
        if day in known:
            hit = True
        else:
            try:
                hit = trading_calendar.is_trading_day(day) is True
            except Exception:  # noqa: BLE001 日历意外异常按「未知」处理：只认已有数据的日期
                hit = False
        if hit:
            axis.append(day)
            if len(axis) >= n:
                break
        day -= timedelta(days=1)
    axis.reverse()
    return axis


def _snapshot_axis_rows(kind_db, n, end_date=None, before=None):
    """按交易日轴读出某类快照：[(trade_date, payload | None)] 升序；无快照返回 []。

    - 轴末点：end_date；缺省时取 before 之前（不含）或全表的最新快照日；
    - 轴起点不早于该类最早一行快照（「按现有可得长度」，不在头部堆 None）；
    - 轴上没有快照的交易日 payload 为 None（缺口如实保留，不压缩时间）。
    """
    qs = MarketDailySnapshot.objects.filter(kind=kind_db)
    if before is not None:
        qs = qs.filter(trade_date__lt=before)
    if end_date is None:
        end_date = qs.order_by('-trade_date').values_list('trade_date', flat=True).first()
        if end_date is None:
            return []
    span = n * AXIS_CALENDAR_MULTIPLIER + AXIS_CALENDAR_BUFFER
    known = list(
        qs.filter(trade_date__gte=end_date - timedelta(days=span), trade_date__lte=end_date)
        .values_list('trade_date', flat=True)
    )
    if not known:
        return []
    first = min(known)
    axis = [d for d in trading_day_axis(end_date, n, known) if d >= first]
    if not axis:
        return []
    by_date = dict(
        qs.filter(trade_date__gte=axis[0], trade_date__lte=axis[-1])
        .values_list('trade_date', 'payload')
    )
    return [(d, by_date.get(d)) for d in axis]


def _margin_value(payload_row):
    """快照行里的融资余额（亿元，#9）；口径不可信时返回 None。

    旧版落库把缺失的单边 rz 按 0 相加（只剩沪市融资余额），会被误读成余额腰斩：
    融资余额应占融资融券余额的绝大部分（实际约 98%~99%），比值不在 [0.9, 1]
    之间视为该行 rz 不可信，如实缺失而不是冒充。
    """
    if not payload_row:
        return None
    value = payload_row.get(MARGIN_VALUE_KEY)
    total = payload_row.get('total')
    if value is None:
        return None
    if total and not (0.9 <= value / total <= 1.0):
        return None
    return value


def load_margin_series(index_as_of, n=MARGIN_CONTEXT_DAYS):
    """沪深300 的融资余额序列：按交易日轴对齐，末点 = 指数数据日的前一交易日（#8/#9）。

    两融 T+1 披露：扫描日 T（17:15）最新只有 T−1 的数，所以轴末点固定为
    index_as_of 的前一交易日，且只读 index_as_of 之前（不含）的快照——事后重算
    （信号详情页）时也不会混入 T 及以后的数。T−1 尚未披露时末点为 None，维度
    如实不可用，而不是拿更早的数冒充最新。
    返回 (items, margin_as_of)：items 为 [{'date', 'rz', 'total'}]（缺失日两者为
    None），margin_as_of 为最后一个有值的数据日（无则 None）。
    """
    if isinstance(index_as_of, str):
        index_as_of = _date.fromisoformat(index_as_of)
    kind = MarketDailySnapshot.KIND_MARGIN
    known = list(
        MarketDailySnapshot.objects.filter(kind=kind, trade_date__lt=index_as_of)
        .order_by('-trade_date').values_list('trade_date', flat=True)[:1]
    )
    prev = trading_day_axis(index_as_of - timedelta(days=1), 1, known)
    if not prev:
        return [], None
    rows = _snapshot_axis_rows(kind, n, end_date=prev[0], before=index_as_of)
    items, margin_as_of = [], None
    for trade_date, payload in rows:
        row = (payload or [None])[0] if payload else None
        value = _margin_value(row)
        items.append({
            'date': trade_date.isoformat(),
            'rz': value,
            'total': row.get('total') if row else None,
        })
        if value is not None:
            margin_as_of = trade_date.isoformat()
    # 末点之后（T−1 未披露）时轴末点仍保留为 None：维度据此如实不可用
    if rows and rows[-1][0] != prev[0]:
        items.append({'date': prev[0].isoformat(), 'rz': None, 'total': None})
    return items, margin_as_of


def build_index_context(code, name):
    """指数上下文：新浪日线 OHLCV（同一上游接口，见 market.indices）+ 融资余额快照序列。

    日线取 CONTEXT_BARS 根（#10）：上游一次返回全史，取多长不增加请求。
    """
    from ..market import indices

    data = indices.fetch_index_daily_series(code, days=CONTEXT_BARS)
    if not data.get('available'):
        return None, data.get('message') or '指数日线不可用'
    items = data['items']
    if len(items) < MIN_PRICE_BARS:
        return None, f'指数日线仅 {len(items)} 根，历史不足'

    ctx = _price_ctx('index', code, name, items)
    ctx['margin'] = []
    ctx['margin_as_of'] = None
    if code not in MARGIN_VOTE_CODES:
        return ctx, ''  # 只有沪深300用融资余额（ALG-09），其余指数不必读快照
    # 融资余额序列：本站日度快照积累（T+1 披露，可用 backfill_margin 回补），给
    # margin_north 维度用；积累不足时维度自行 available=False，不影响其他维度。
    ctx['margin'], ctx['margin_as_of'] = load_margin_series(ctx['as_of'])
    return ctx, ''


def build_stock_context(stock):
    """自选股上下文：DailyQuote（本站已拉取的日线），零新增上游请求。

    只用已收盘的日线（DAT-13）：盘中拉到的当日 bar 量价不完整，混进序列会
    让振幅/量比/均线距离按半天数据投票。取最近 CONTEXT_BARS 根（#10），
    本地不足就用现有的，不为补历史额外联网。
    """
    rows = list(
        DailyQuote.objects.filter(stock=stock, is_final=True)
        .order_by('-trade_date')
        .values_list(
            'trade_date', 'open_price', 'high_price', 'low_price',
            'close_price', 'volume', 'turnover',
        )[:CONTEXT_BARS]
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
            'amount': float(r[6]) if r[6] is not None else None,
        }
        for r in rows
    ]
    ctx = _price_ctx('stock', stock.code, stock.name, items)
    # 成交额（元）只用于识别量价基准断层（ALG-13），不参与其他维度
    ctx['amount'] = [it['amount'] for it in items]
    # 逐日流通股本（股）：换手率维度用真实换手率；None 表示股本数据缺失（ALG-13）
    ctx['float_shares'] = _float_shares_for(stock.code, ctx['dates'])
    return ctx, ''


def _float_shares_for(code, dates):
    """与 dates 对齐的流通股本序列；取不到（关闭/熔断/上游失败）返回 None。"""
    from ..datasources import float_shares

    history = float_shares.fetch_float_share_history(code)
    if not history:
        return None
    return float_shares.shares_on_dates(history, dates)


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


def _sector_kind_db(kind):
    return (
        MarketDailySnapshot.KIND_INDUSTRY_FF
        if kind == 'industry'
        else MarketDailySnapshot.KIND_CONCEPT_FF
    )


def load_sector_snapshots(kind, lookback_days=SECTOR_CONTEXT_DAYS, end_date=None):
    """一次读出某类板块最近 lookback_days 个交易日的快照并按名字建索引（PERF-04）。

    返回 [(trade_date, {name: row})]，日期升序。扫描时每类只读一次、全部板块
    共用；旧实现每个板块各自重读 45 天全量 JSON，一次扫描约 2 万次大 JSON
    反序列化。
    日期轴按交易日历生成（#8）：轴末点为 end_date 或最新快照日，轴上缺快照的
    交易日为 {}（各板块该日即 None），不再「最近 N 行」压缩时间；轴起点不早于
    最早一行快照（按现有可得长度）。
    """
    out = []
    for trade_date, payload in _snapshot_axis_rows(_sector_kind_db(kind), lookback_days, end_date):
        out.append((trade_date, {r.get('name'): r for r in (payload or []) if r.get('name')}))
    return out


def build_sector_context(kind, name, lookback_days=SECTOR_CONTEXT_DAYS, snapshots=None):
    """板块上下文：本站日度快照透视出的逐日 net / change_pct 序列。

    「某板块某天缺行就 None」：回填可能只补到部分板块，绝不能拿旧值或 0
    补天（与快照模块「窗口齐全才算数」同一纪律）；各维度在取窗时遇 None
    自行如实不可用。日期轴是交易日历（#8），整天缺快照同样是 None。
    snapshots：load_sector_snapshots 的结果（扫描时预加载、各板块共用）；
    缺省时自行读库（信号详情页等单板块场景）。
    """
    if snapshots is None:
        snapshots = load_sector_snapshots(kind, lookback_days)
    dates, nets, pcts = [], [], []
    valid = 0
    for trade_date, by_name in snapshots:
        row = by_name.get(name)
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

    base = [w for w in widths[:-1] if w is not None][-int(params.get('noise_window', NOISE_DEFAULTS['noise_window'])):]
    need = int(params.get('noise_min', NOISE_DEFAULTS['noise_min']))
    if len(base) < need:
        return _unavailable(f'波动基线样本不足（{len(base)}/{need}）')
    thr = _percentile(base, params.get('compress_pct', COMPRESS_PCT_DEFAULT))
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


def _share_basis_break(ctx, lookback, ratio, k=3):
    """量价基准断层（送转/配股等股本变动）检测：返回断层所在日期或 None（ALG-13）。

    原理：日线是前复权价，成交量不复权。r = 成交额 / (成交量 × 收盘价) 在正常
    交易日约等于「均价/收盘价」，常年在 1 附近小幅波动；送转（如 10 转 10）
    之前的前复权价被整体压低、成交量仍是旧股本口径，r 会在除权日前后出现
    台阶（10 转 10 约 2 倍）。比较每个点前后各 k 日 r 的中位数，台阶超过 ratio
    倍即判为断层。只覆盖送转/配股这类改变复权因子的事件；定增解禁等不改价格
    的流通股本变动需要真实流通股本数据，本站暂无（见 P2 说明）。
    成交额缺失太多时无法判断，返回 None（不因缺数据而停用维度）。
    """
    amounts, volumes, closes = ctx.get('amount'), ctx.get('volume'), ctx.get('close')
    if not (amounts and volumes and closes):
        return None
    n = len(closes)
    start = max(0, n - lookback)
    r = []
    for i in range(start, n):
        a, v, c = amounts[i], volumes[i], closes[i]
        r.append(a / (v * c) if a and v and c else None)

    def med(vals):
        vals = sorted(x for x in vals if x is not None)
        return vals[len(vals) // 2] if len(vals) >= 2 else None

    for j in range(k, len(r) - k + 1):
        before, after = med(r[j - k:j]), med(r[j:j + k])
        if before and after and max(before, after) / min(before, after) >= ratio:
            return ctx['dates'][start + j]
    return None


# 流通股本缺失时附在换手率维度 note 上的标注（ALG-13）
SHARE_DATA_MISSING_NOTE = '股本数据缺失：仅能识别送转类断层，定增/解禁等股本变动无法识别'
# 断层附近流通股本变化超过该比例，才算股本数据「解释了」这次断层
_SHARE_CHANGE_TOL = 0.02


def _turnover_series(ctx, window):
    """真实换手率序列 = 成交量 / 当日流通股本（ALG-13）。

    末尾 window 根里有成交量却缺股本的，整体视为股本数据缺失返回 None：
    一半换手率一半成交量混在一个分位窗口里没有意义。成交量与股本都是
    未复权口径，送转前后各自一致，比值天然连续。
    """
    volumes, shares = ctx.get('volume'), ctx.get('float_shares')
    if not volumes or not shares or len(shares) != len(volumes):
        return None
    out = [v / s if v is not None and s else None for v, s in zip(volumes, shares)]
    tail = range(max(0, len(volumes) - window), len(volumes))
    if any(volumes[i] is not None and out[i] is None for i in tail):
        return None
    return out


def _share_changed_near(ctx, broke_date, k=3):
    """断层日前后 k 根内流通股本是否有超过 _SHARE_CHANGE_TOL 的变化。"""
    dates, shares = ctx['dates'], ctx['float_shares']
    j = dates.index(broke_date)
    near = [s for s in shares[max(0, j - k):j + k] if s]
    return len(near) >= 2 and max(near) / min(near) - 1 > _SHARE_CHANGE_TOL


def _join_note(note, extra):
    if not extra:
        return note
    return f'{note}；{extra}' if note else extra


def eval_turnover_pct(ctx, params):
    """换手率分位（指数/个股）：成交量自身 60 日分位的 5 日变化。

    口径说明（ALG-13）：个股有流通股本数据时用真实换手率（成交量 / 当日
    流通股本）的分位，送转、定增、解禁前后都连续；股本数据缺失时退回
    成交量分位（窗口内股本不变时两者单调等价），检测到送转/配股导致的
    量价基准断层就如实不可用，未检测到也在 note 标注「股本数据缺失」——
    定增解禁这类不改价格的股本变动此时识别不了。指数无股本概念，用成交量
    分位作为市场活跃度代理。
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
    share_note = ''
    if ctx.get('kind') == 'stock':
        broke = _share_basis_break(ctx, window, params.get('split_ratio', SPLIT_RATIO_DEFAULT))
        turnover = _turnover_series(ctx, window)
        if turnover is not None:
            # 有股本数据：断层若能被股本变化解释（送转/配股），真实换手率照常可用；
            # 断层附近股本却没变，说明两份数据对不上，宁可暂停也不按错的口径投票
            if broke and not _share_changed_near(ctx, broke):
                return _unavailable(f'{broke} 前后量价基准断层，但流通股本未见变动，数据不一致，换手率分位暂停')
            volumes = turnover
        else:
            # 股本数据缺失：退回成交量分位。窗口内出现送转/配股时成交量整体抬升
            # 一个台阶，分位会把股本扩大误读成关注度回升，维度暂停直到断层滚出窗口
            if broke:
                return _unavailable(f'{broke} 前后疑似送转/股本变动（量价基准断层），换手率分位暂停至滚出窗口')
            share_note = SHARE_DATA_MISSING_NOTE

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
        value, direction, _join_note(note, share_note),
        _series_points(ctx['dates'], pct_series),
    )


def eval_margin_north(ctx, params):
    """融资余额渐进变化（仅沪深300）。

    口径说明（#9）：用融资余额 rz（沪+深），不是融资融券余额；序列按交易日
    历对齐，末点是指数数据日 T 的前一交易日（两融 T+1 披露），缺失日为 None。
    3 日均值要求窗口齐全；6 个点只够算出第一个漂移，噪声闸门还需 noise_min
    个历史漂移，所以实际约需 21 个交易日的连续积累才投票（可用 backfill_margin
    命令回补历史）。北向资金自 2024-08 起停止逐日披露，本维度与北向无关。
    """
    if ctx.get('kind') == 'index' and ctx.get('code') not in MARGIN_VOTE_CODES:
        return _unavailable('融资余额为沪深合计口径，仅在沪深300上投票（避免各指数重复计票）')
    margin = ctx.get('margin') or []
    if len(margin) < 6:
        return _unavailable('融资余额快照积累不足（T+1 披露，逐日积累中）')
    totals = [m.get(MARGIN_VALUE_KEY) for m in margin]
    m3 = _rolling_mean_series(totals, 3, strict=True)
    if m3[-1] is None or m3[-4] is None or not m3[-4]:
        return _unavailable('融资余额均值窗口不齐')
    raws = [
        (m3[i] / m3[i - 3] - 1) * 100 if i >= 3 and m3[i] is not None and m3[i - 3] else None
        for i in range(len(m3))
    ]
    raw = raws[-1]
    value, direction, note = _gated(raw, raws[:-1], params)
    if direction is not None:
        note = f"融资余额口径，数据日 {margin[-1]['date']}（T−1）"
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
