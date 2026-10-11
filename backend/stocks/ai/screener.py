"""条件选股引擎：结构化条件 DSL + 在最新日线数据上执行。

DSL 示例：
{
  "logic": "all",              # all=AND / any=OR
  "conditions": [
    {"field": "change_pct", "op": "gt", "value": 3},
    {"field": "turnover", "op": "between", "value": [50000000, 2000000000]},
    {"field": "above_ma20", "op": "eq", "value": 1}
  ],
  "order_by": "change_pct",    # 可选
  "order_dir": "desc",         # asc/desc，默认 desc
  "limit": 20                  # 可选
}

数据全部来自 DailyQuote 真实落库数据；多日字段（均线/区间涨跌/量比/
技术指标等）由截至 as_of 的最近 ≤MAX_LOOKBACK（140）根已收盘日线现算。
任何字段因历史不足或数据缺失取不到值时为 None，该股此条件不命中（如实的
「未知」，不当 0，遵守数据诚信红线）。

横截面口径（ALG-12）：先确定全局 as_of = 自选股已收盘日线的最新交易日，只有
最新一根就是 as_of 的股票参与筛选；停牌/拉取失败而停在更早日期的股票不再拿
旧行情混进同一张榜，列入 excluded 并告知用户（见 run_screener_with_meta）。
"""
from dataclasses import dataclass

from ..models import Stock

# 计算多日指标所需的最大回看根数（MACD 的 EMA26+DEA9 收敛需要较长预热）
MAX_LOOKBACK = 140

# 布尔类字段（值 1=成立 / 0=不成立），前端渲染为「是/否」下拉
BOOL_FIELDS = {
    'above_ma5', 'above_ma10', 'above_ma20', 'above_ma60',
    'ma5_gt_ma10', 'ma5_gt_ma20', 'ma10_gt_ma20',
    'new_high_20d', 'new_low_20d', 'new_high_60d', 'new_low_60d',
    'macd_dif_gt_dea', 'macd_golden_cross', 'macd_dead_cross', 'macd_dif_gt_zero',
    'kdj_golden_cross', 'kdj_dead_cross',
    'above_boll_mid', 'above_boll_upper', 'below_boll_lower',
}

OPS = {'gt', 'gte', 'lt', 'lte', 'eq', 'between'}


@dataclass
class ConditionError(Exception):
    message: str

    def __str__(self):
        return self.message


def _f(dec):
    return float(dec) if dec is not None else None


def _closes(ctx, n):
    """最近 n 根收盘价（float 列表，升序，尾元素最新）；根数不足或含缺失返回 None。"""
    bars = ctx['bars'][-n:]
    if len(bars) < n:
        return None
    closes = [_f(b.close_price) for b in bars]
    if any(c is None for c in closes) or closes[-1] is None:
        return None
    return closes


def _pct_n(n):
    """区间涨跌幅：最新收盘 / n 根前收盘 - 1。"""
    def getter(ctx):
        closes = _closes(ctx, n + 1)
        if not closes or closes[0] == 0:
            return None
        return (closes[-1] / closes[0] - 1) * 100
    return getter


def _above_ma(n):
    """收盘价站上 n 日均线（>=）。"""
    def getter(ctx):
        closes = _closes(ctx, n)
        if not closes:
            return None
        ma = sum(closes) / n
        return 1 if closes[-1] >= ma else 0
    return getter


def _ma_gt(short, long):
    """短均线严格在长均线上方。"""
    def getter(ctx):
        closes = _closes(ctx, long)
        if not closes:
            return None
        ma_short = sum(closes[-short:]) / short
        ma_long = sum(closes) / long
        return 1 if ma_short > ma_long else 0
    return getter


def _new_extreme(n, high):
    """当日最高价/最低价创最近 n 根（含当日）的新高/新低（ALG-12）。

    行情软件的「创 N 日新高」看最高价而不是收盘价；并列不算新高（必须严格
    高于此前 n-1 根的最高价，新低对称）。
    """
    def getter(ctx):
        bars = ctx['bars'][-n:]
        if len(bars) < n:
            return None
        vals = [_f(b.high_price if high else b.low_price) for b in bars]
        if any(v is None for v in vals):
            return None
        last, rest = vals[-1], vals[:-1]
        if high:
            return 1 if last > max(rest) else 0
        return 1 if last < min(rest) else 0
    return getter


def _volume_ratio(ctx):
    """量比：今日成交量 / 前 5 根均量。"""
    bars = ctx['bars']
    if len(bars) < 6:
        return None
    today = _f(bars[-1].volume)
    base = [_f(b.volume) for b in bars[-6:-1]]
    if not today or any(v is None or v == 0 for v in base):
        return None
    return today / (sum(base) / 5)


def _turnover_5d_avg(ctx):
    """近 5 根（含最新）日均成交额（元）。"""
    bars = ctx['bars']
    if len(bars) < 5:
        return None
    vals = [_f(b.turnover) for b in bars[-5:]]
    if any(v is None for v in vals):
        return None
    return sum(vals) / 5


def _pct_from_high_20d(ctx):
    """距最近 20 根最高收盘的回撤（≤0，0 表示正在新高）。"""
    closes = _closes(ctx, 20)
    if not closes:
        return None
    peak = max(closes)
    if peak == 0:
        return None
    return (closes[-1] / peak - 1) * 100


def _up_days(ctx):
    """连续上涨天数：从最新一根往前数收盘连涨（相等即断）。"""
    bars = ctx['bars']
    if len(bars) < 2:
        return None
    closes = [_f(b.close_price) for b in bars]
    if closes[-1] is None or closes[-2] is None:
        return None
    days = 0
    i = len(closes) - 1
    while i >= 1 and closes[i] is not None and closes[i - 1] is not None:
        if closes[i] > closes[i - 1]:
            days += 1
            i -= 1
        else:
            break
    return days


# ============================================================
# 技术指标（MACD / KDJ / RSI / BOLL）
# 全部由落库日线现算；参数与国内软件常用口径一致。
# 历史根数不足以稳定收敛时不给值（None），不硬算。
# ============================================================

# MACD 预热（ALG-12）：EMA 以首值为种子，35 根时 EMA26 的种子残差仍约 7%，
# 金叉/零轴判定可能与行情软件不一致；120 根时残差 < 0.01%
_MACD_MIN_BARS = 120
_KDJ_MIN_BARS = 12    # RSV9 + K/D 递推预热
_BOLL_N = 20
# BOLL 标准差口径（ALG-12）：ddof=0 即总体标准差（除以 N），按终审报告的建议
# 与通达信 BOLL 一致；各软件口径不一（部分资料称通达信 STD 为样本标准差），
# 改口径只需改这里
_BOLL_DDOF = 0


def _ema_series(values, n):
    """EMA（首值作种子，α=2/(n+1)）。"""
    alpha = 2.0 / (n + 1)
    out = [values[0]]
    ema = values[0]
    for v in values[1:]:
        ema = alpha * v + (1 - alpha) * ema
        out.append(ema)
    return out


def _indicators(ctx):
    """一次性算好全部技术指标并记忆在 ctx['ind']；返回 dict（可能部分为 None）。"""
    if ctx.get('ind') is not None:
        return ctx['ind']
    ind = {}
    bars = ctx['bars']
    closes = [_f(b.close_price) for b in bars]
    highs = [_f(b.high_price) for b in bars]
    lows = [_f(b.low_price) for b in bars]

    # ---- MACD(12, 26, 9)，柱 = 2*(DIF-DEA)（国内口径） ----
    if len(closes) >= _MACD_MIN_BARS and None not in closes:
        ema12 = _ema_series(closes, 12)
        ema26 = _ema_series(closes, 26)
        dif = [a - b for a, b in zip(ema12, ema26)]
        dea = _ema_series(dif, 9)
        ind['macd_dif'] = dif[-1]
        ind['macd_dea'] = dea[-1]
        ind['macd_hist'] = 2 * (dif[-1] - dea[-1])
        ind['macd_dif_gt_dea'] = 1 if dif[-1] >= dea[-1] else 0
        ind['macd_dif_gt_zero'] = 1 if dif[-1] > 0 else 0
        ind['macd_golden_cross'] = 1 if dif[-1] >= dea[-1] and dif[-2] < dea[-2] else 0
        ind['macd_dead_cross'] = 1 if dif[-1] <= dea[-1] and dif[-2] > dea[-2] else 0
    else:
        ind.update({k: None for k in (
            'macd_dif', 'macd_dea', 'macd_hist',
            'macd_dif_gt_dea', 'macd_golden_cross', 'macd_dead_cross',
            'macd_dif_gt_zero',
        )})

    # ---- KDJ(9, 3, 3)，SMA(X,3,1) 递推，K/D 种子 50 ----
    if len(closes) >= _KDJ_MIN_BARS and None not in closes \
            and None not in highs and None not in lows:
        k, d = 50.0, 50.0
        for i in range(len(bars)):
            hh = max(highs[max(0, i - 8):i + 1])
            ll = min(lows[max(0, i - 8):i + 1])
            rsv = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100
            k = (2 * k + rsv) / 3
            d = (2 * d + k) / 3
        ind['kdj_k'] = k
        ind['kdj_d'] = d
        ind['kdj_j'] = 3 * k - 2 * d
        # 金叉/死叉用倒数第二根状态
        k2, d2 = 50.0, 50.0
        for i in range(len(bars) - 1):
            hh = max(highs[max(0, i - 8):i + 1])
            ll = min(lows[max(0, i - 8):i + 1])
            rsv = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100
            k2 = (2 * k2 + rsv) / 3
            d2 = (2 * d2 + k2) / 3
        ind['kdj_golden_cross'] = 1 if k >= d and k2 < d2 else 0
        ind['kdj_dead_cross'] = 1 if k <= d and k2 > d2 else 0
    else:
        ind.update({k: None for k in (
            'kdj_k', 'kdj_d', 'kdj_j', 'kdj_golden_cross', 'kdj_dead_cross',
        )})

    # ---- RSI(6 / 14)，Wilder 平滑 ----
    def _rsi(n):
        if len(closes) < n + 1:
            return None
        gains, losses = 0.0, 0.0
        # 先用前 n 个差分做种子均值
        deltas = [closes[i] - closes[i - 1] for i in range(1, n + 1)]
        gains = sum(max(d, 0) for d in deltas) / n
        losses = sum(-min(d, 0) for d in deltas) / n
        for i in range(n + 1, len(closes)):
            delta = closes[i] - closes[i - 1]
            gains = (gains * (n - 1) + max(delta, 0)) / n
            losses = (losses * (n - 1) + max(-delta, 0)) / n
        if gains + losses == 0:
            return None
        return gains / (gains + losses) * 100

    ind['rsi_6'] = _rsi(6)
    ind['rsi_14'] = _rsi(14)

    # ---- BOLL(20, 2) ----
    boll_closes = closes[-_BOLL_N:]
    if len(boll_closes) == _BOLL_N and None not in boll_closes:
        mid = sum(boll_closes) / _BOLL_N
        std = (sum((c - mid) ** 2 for c in boll_closes) / (_BOLL_N - _BOLL_DDOF)) ** 0.5
        last = closes[-1]
        ind['above_boll_mid'] = 1 if last >= mid else 0
        ind['above_boll_upper'] = 1 if last >= mid + 2 * std else 0
        ind['below_boll_lower'] = 1 if last <= mid - 2 * std else 0
    else:
        ind.update({k: None for k in (
            'above_boll_mid', 'above_boll_upper', 'below_boll_lower',
        )})

    ctx['ind'] = ind
    return ind


def _macd(field):
    def getter(ctx):
        return _indicators(ctx).get(field)
    return getter


def _kdj(field):
    def getter(ctx):
        return _indicators(ctx).get(field)
    return getter


def _rsi(field):
    def getter(ctx):
        return _indicators(ctx).get(field)
    return getter


def _boll(field):
    def getter(ctx):
        return _indicators(ctx).get(field)
    return getter


def _valuation(field):
    """估值字段：来自腾讯实时快照（ctx['val']），上游不可用即 None。"""
    def getter(ctx):
        val = ctx.get('val') or {}
        return val.get(field)
    return getter


# 可筛选字段：key -> (中文名, 取值函数, 说明)
# 取值函数输入 ctx（dict：quote=最新日线, bars=升序日线列表），返回 float/None
FIELDS = {
    # ---- 当日字段（来自最新一根日线） ----
    'close_price': ('收盘价', lambda ctx: _f(ctx['quote'].close_price), '元'),
    'open_price': ('开盘价', lambda ctx: _f(ctx['quote'].open_price), '元'),
    'high_price': ('最高价', lambda ctx: _f(ctx['quote'].high_price), '元'),
    'low_price': ('最低价', lambda ctx: _f(ctx['quote'].low_price), '元'),
    'change_pct': ('涨跌幅', lambda ctx: _f(ctx['quote'].change_pct), '%，相对昨收'),
    'open_close_pct': ('日内涨幅', lambda ctx: _f(ctx['quote'].open_close_pct), '%，(收盘-开盘)/开盘'),
    'high_low_pct': ('日内振幅', lambda ctx: _f(ctx['quote'].high_low_pct), '%，(最高-最低)/最低'),
    'volume': ('成交量', lambda ctx: _f(ctx['quote'].volume), '股'),
    'turnover': ('成交额', lambda ctx: _f(ctx['quote'].turnover), '元'),
    # ---- 多日衍生字段（由截至 as_of 的最近 ≤140 根日线现算） ----
    'pct_5d': ('5日涨跌幅', _pct_n(5), '%'),
    'pct_10d': ('10日涨跌幅', _pct_n(10), '%'),
    'pct_20d': ('20日涨跌幅', _pct_n(20), '%'),
    'pct_60d': ('60日涨跌幅', _pct_n(60), '%'),
    'volume_ratio': ('量比(今量/5日均量)', _volume_ratio, '倍'),
    'turnover_5d_avg': ('5日日均成交额', _turnover_5d_avg, '元'),
    'pct_from_high_20d': ('距20日最高收盘回撤', _pct_from_high_20d, '%，≤0'),
    'up_days': ('连续上涨天数', _up_days, '天'),
    'above_ma5': ('收盘站上5日线', _above_ma(5), '1是/0否'),
    'above_ma10': ('收盘站上10日线', _above_ma(10), '1是/0否'),
    'above_ma20': ('收盘站上20日线', _above_ma(20), '1是/0否'),
    'above_ma60': ('收盘站上60日线', _above_ma(60), '1是/0否'),
    'ma5_gt_ma10': ('5日线在10日线上方', _ma_gt(5, 10), '1是/0否'),
    'ma5_gt_ma20': ('5日线在20日线上方', _ma_gt(5, 20), '1是/0否'),
    'new_high_20d': ('创20日新高', _new_extreme(20, high=True), '1是/0否，最高价严格高于前19日'),
    'new_low_20d': ('创20日新低', _new_extreme(20, high=False), '1是/0否，最低价严格低于前19日'),
    'new_high_60d': ('创60日新高', _new_extreme(60, high=True), '1是/0否，最高价严格高于前59日'),
    'new_low_60d': ('创60日新低', _new_extreme(60, high=False), '1是/0否，最低价严格低于前59日'),
    'ma10_gt_ma20': ('10日线在20日线上方', _ma_gt(10, 20), '1是/0否'),
    # ---- 技术指标（由落库日线现算，国内常用口径） ----
    'macd_dif': ('MACD DIF', _macd('macd_dif'), '值'),
    'macd_dea': ('MACD DEA', _macd('macd_dea'), '值'),
    'macd_hist': ('MACD柱', _macd('macd_hist'), '值'),
    'macd_dif_gt_dea': ('DIF在DEA上方', _macd('macd_dif_gt_dea'), '1是/0否'),
    'macd_golden_cross': ('MACD金叉', _macd('macd_golden_cross'), '1是/0否'),
    'macd_dead_cross': ('MACD死叉', _macd('macd_dead_cross'), '1是/0否'),
    'macd_dif_gt_zero': ('DIF在零轴上方', _macd('macd_dif_gt_zero'), '1是/0否'),
    'kdj_k': ('KDJ K值', _kdj('kdj_k'), '值'),
    'kdj_d': ('KDJ D值', _kdj('kdj_d'), '值'),
    'kdj_j': ('KDJ J值', _kdj('kdj_j'), '值'),
    'kdj_golden_cross': ('KDJ金叉', _kdj('kdj_golden_cross'), '1是/0否'),
    'kdj_dead_cross': ('KDJ死叉', _kdj('kdj_dead_cross'), '1是/0否'),
    'rsi_6': ('RSI(6)', _rsi('rsi_6'), '值0-100'),
    'rsi_14': ('RSI(14)', _rsi('rsi_14'), '值0-100'),
    'above_boll_mid': ('收盘在布林中轨上方', _boll('above_boll_mid'), '1是/0否'),
    'above_boll_upper': ('收盘触及布林上轨', _boll('above_boll_upper'), '1是/0否'),
    'below_boll_lower': ('收盘触及布林下轨', _boll('below_boll_lower'), '1是/0否'),
    # ---- 估值快照（腾讯实时行情，上游不可用即无值） ----
    'turnover_rate': ('换手率', _valuation('turnover_rate'), '%'),
    'pe_ttm': ('市盈率(TTM)', _valuation('pe_ttm'), '倍'),
    'pb': ('市净率', _valuation('pb'), '倍'),
    'float_mv': ('流通市值', _valuation('float_mv'), '元'),
    'total_mv': ('总市值', _valuation('total_mv'), '元'),
}


def _latest_contexts():
    """参与筛选的 (stock, ctx) 列表与横截面元信息。

    返回 (contexts, meta)：meta = {'as_of': date|None, 'excluded': [...]}。
    - 只用已收盘的日线（DAT-13）：盘中拉到的当日 bar 价量都不完整；
    - as_of 取全部活跃股票已收盘日线的最新交易日，最新一根不是 as_of 的股票
      不参与，放进 excluded（ALG-12）；
    - 查询批量化（PERF-08）：一次聚合取各股最新日期，一次取全部参与股票的
      回看窗口，内存里分组；旧实现每只股票两次查询。
    估值快照（腾讯）对参与股票一次批量拉取，随 ctx 分发。
    """
    from django.db.models import Max

    from ..models import DailyQuote
    from .valuation import fetch_valuation_map

    stocks = {s.id: s for s in Stock.objects.filter(is_active=True)}
    if not stocks:
        return [], {'as_of': None, 'excluded': []}
    final = DailyQuote.objects.filter(stock_id__in=list(stocks), is_final=True)
    latest = dict(final.values('stock_id').annotate(last=Max('trade_date')).values_list('stock_id', 'last'))
    as_of = max(latest.values()) if latest else None

    excluded = []
    for sid, stock in sorted(stocks.items(), key=lambda kv: kv[1].code):
        last = latest.get(sid)
        if last != as_of or as_of is None:
            excluded.append({
                'stock_id': sid, 'code': stock.code, 'name': stock.name,
                'last_trade_date': last.isoformat() if last else None,
                'reason': '无已收盘日线' if last is None else f'最新已收盘日线停在 {last.isoformat()}',
            })
    ids = [sid for sid, last in latest.items() if last == as_of]
    meta = {'as_of': as_of, 'excluded': excluded}
    if not ids:
        return [], meta

    # 回看窗口：参与股票共同的最近 MAX_LOOKBACK 个交易日（个股有停牌缺口时根数会少于
    # MAX_LOOKBACK，指标按实际根数与各自的最少根数要求处理）
    dates = list(
        final.filter(stock_id__in=ids, trade_date__lte=as_of)
        .values_list('trade_date', flat=True).distinct().order_by('-trade_date')[:MAX_LOOKBACK]
    )
    bars_by_stock = {sid: [] for sid in ids}
    for bar in final.filter(stock_id__in=ids, trade_date__gte=dates[-1], trade_date__lte=as_of) \
            .order_by('stock_id', 'trade_date'):
        bars_by_stock[bar.stock_id].append(bar)

    participants = sorted((stocks[sid] for sid in ids), key=lambda s: s.code)
    val_map = fetch_valuation_map([s.code for s in participants])
    result = []
    for stock in participants:
        bars = bars_by_stock[stock.id]
        ctx = {'stock': stock, 'quote': bars[-1], 'bars': bars, 'val': val_map.get(stock.code)}
        result.append((stock, ctx))
    return result, meta


def _match(op, value, target):
    if op == 'between':
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ConditionError('between 操作符的 value 必须是 [下限, 上限]')
        lo, hi = float(value[0]), float(value[1])
        return lo <= target <= hi
    if op == 'gt':
        return target > value
    if op == 'gte':
        return target >= value
    if op == 'lt':
        return target < value
    if op == 'lte':
        return target <= value
    if op == 'eq':
        return target == value
    raise ConditionError(f'不支持的操作符：{op}')


def _validate(spec):
    if not isinstance(spec, dict):
        raise ConditionError('筛选条件必须是 JSON 对象')
    logic = spec.get('logic', 'all')
    if logic not in ('all', 'any'):
        raise ConditionError('logic 只能是 all（且）或 any（或）')
    conditions = spec.get('conditions')
    if not isinstance(conditions, list) or not conditions:
        raise ConditionError('conditions 必须是非空数组')
    for cond in conditions:
        if not isinstance(cond, dict):
            raise ConditionError('每个条件必须是对象')
        field = cond.get('field')
        op = cond.get('op')
        if field not in FIELDS:
            raise ConditionError(
                f'不支持的字段：{field}，可选：{", ".join(FIELDS)}'
            )
        if op not in OPS:
            raise ConditionError(f'不支持的操作符：{op}，可选：{", ".join(sorted(OPS))}')
        if 'value' not in cond:
            raise ConditionError('条件缺少 value')
        if op != 'between' and not isinstance(cond['value'], (int, float)):
            raise ConditionError(f'{field} {op} 的 value 必须是数字')
        if field in BOOL_FIELDS and op != 'between' \
                and cond['value'] not in (0, 1):
            raise ConditionError(f'{field} 是布尔字段，value 只能是 0 或 1')
    order_by = spec.get('order_by')
    if order_by is not None and order_by not in FIELDS:
        raise ConditionError(f'排序字段不支持：{order_by}')


def run_screener(spec):
    """执行筛选，返回结果列表（含股票信息与命中的字段值）"""
    rows, _meta = run_screener_with_meta(spec)
    return rows


def _sort_key(order_by, desc):
    """排序键：无值的行无论升序降序都排在最后（ALG-12）。

    旧实现用 -inf 代替 None，升序时无值的股票排在最前面。
    """
    def key(row):
        v = row.get(order_by)
        if v is None:
            return (1, 0.0)
        return (0, -v if desc else v)
    return key


def run_screener_with_meta(spec):
    """执行筛选，返回 (rows, meta)；meta 含 as_of 与未参与股票列表（excluded）。"""
    _validate(spec)
    logic = spec.get('logic', 'all')
    conditions = spec['conditions']

    contexts, meta = _latest_contexts()
    rows = []
    for stock, ctx in contexts:
        values = {key: fn(ctx) for key, (_, fn, _) in FIELDS.items()}

        results = []
        for cond in conditions:
            target = values[cond['field']]
            # 该股此项无数据 → 该条件不命中（如实的"未知"，不当作 0）
            if target is None:
                results.append(False)
                continue
            try:
                results.append(_match(cond['op'], cond['value'], target))
            except (TypeError, ValueError):
                raise ConditionError(f"条件 {cond['field']} {cond['op']} 的 value 类型不合法")
        matched = all(results) if logic == 'all' else any(results)
        if not matched:
            continue

        row = {
            'stock_id': stock.id,
            'code': stock.code,
            'name': stock.name,
            'trade_date': ctx['quote'].trade_date.isoformat(),
        }
        row.update({k: values[k] for k in values if values[k] is not None})
        rows.append(row)

    order_by = spec.get('order_by')
    if order_by:
        desc = spec.get('order_dir', 'desc') != 'asc'
        rows.sort(key=_sort_key(order_by, desc))
    limit = spec.get('limit')
    if isinstance(limit, int) and limit > 0:
        rows = rows[:limit]
    as_of = meta['as_of']
    return rows, {'as_of': as_of.isoformat() if as_of else None, 'excluded': meta['excluded']}
