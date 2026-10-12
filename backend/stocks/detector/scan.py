"""探测器扫描编排：一个交易日一次完整扫描，产出信号、热力图矩阵与运行日志。

流程（每交易日 17:15 由调度器触发，也可用 run_detector_scan 命令手动执行）：
1. 守卫：非交易日 / 未收盘（15:30 前）直接跳过——与快照落库同一时纪律，
   防止把上一交易日的盘面标成今天（2026-09-07 踩过的坑）；
2. 融资余额快照落库（两融 T+1，探测器是它唯一的日度写入方）；
3. 收集对象宇宙：5 个主要指数 + 全部自选股 + 最近快照出现过的行业/概念板块
   + 成分池（沪深300+中证500，自选以外的成分股，信号 source=pool）；
4. 逐对象：构建序列 → 平静判定（不满足直接跳过）→ 维度评估 → 投票落库；
   板块信号超过每日上限时按强度截断，成分池信号按严重度只推前 N 条，
   并估算期望假阳数（多重检验，#11）；
5. 回看 5 个交易日前信号并按命中率微调权重/阈值（review.py）；
6. upsert DetectorRun（矩阵、降级、调参流水），按保留策略清理旧数据。

成分池（约 800 只）的内存与耗时控制：
- 只扫当日日线已入库（DailyQuote 最新已收盘日 = T）的成分股，其余跳过并在
  DetectorRun.pool_coverage 留痕（缺数代码、未回补代码），不拖垮扫描；
- 分块两遍（DETECTOR_POOL_CHUNK，默认 100 只一块）：第一遍只算 DTC/ID20 原始值
  组成截面参照、随即释放序列；第二遍逐块重建序列、挂分位、评估，块结束即释放。
  峰值内存只与块大小有关，不随成分池线性增长；
- 完整度不足 DETECTOR_POOL_READY_RATIO（默认 0.95）时 pool='auto' 不扫成分池，
  run.pool_status=pending，由调度器在后续重试点补跑（扫描本身幂等，补跑即覆盖）；
  截止点（pool='force'）不再等，有多少扫多少。

幂等性（ALG-08）：同日重跑结果与只跑一次一致——
- 信号行按 (对象, 日期) update_or_create；本轮确定无信号的对象，其当日
  未回看的 watching/active 旧行被删除（降级对象保留旧行，不因上游抖动丢信号）；
- 回看只处理 review_hit 仍为 null 的行，不会双重调参；run 行的调参流水与
  回看计数按「已有 + 本轮」累加，第二次跑不会用空值覆盖第一次的审计记录；
- 落库阶段（信号/清理失效行/回看/run 行）在同一个事务里，中途失败整体
  回滚；网络抓取（序列构建、回看用的指数日线预热）尽量放在事务之外，
  缩短 SQLite 单写锁的持有时间。
"""

from __future__ import annotations

import logging
from datetime import time as dt_time, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..market.indices import TREND_INDICES
from ..market.snapshots import save_margin_snapshot
from ..models import DetectorDimConfig, DetectorRun, DetectorSignal, Stock
from . import calm as calm_mod
from . import registry, review, signals, xsection
from .constants import (
    MARGIN_VOTE_CODES,
    NOISE_DEFAULTS,
    NULL_VOTE_RATE_FALLBACK,
    NULL_VOTE_RATES,
    NULL_VOTE_RATES_BY_KIND,
    POOL_CHUNK_DEFAULT,
    POOL_READY_RATIO_DEFAULT,
    POOL_SIGNAL_CAP_DEFAULT,
    ROLE_DIRECTION,
    SECTOR_SIGNAL_CAP_DEFAULT,
)
from .dimensions import (
    build_index_context,
    build_sector_context,
    build_stock_context,
    load_sector_snapshots,
)

logger = logging.getLogger(__name__)

# 板块宇宙取最近 3 个快照日的名字并集：某板块缺最近一天时不至于整个
# 掉出宇宙（其序列会带 None，由维度评估如实降级），宇宙本身保持稳定。
_SECTOR_UNIVERSE_ROWS = 3
# 删除失效信号时 target_code__in 的分批大小（SQLite 变量数上限）
_CLEAR_BATCH = 500


def load_configs(create=True):
    """注册表种子化 + DB 当前生效值合并，返回扫描用的配置列表。

    为什么每次都合并而不是只读 DB：注册表新增参数键时，旧配置行的
    params 缺键由 defaults 补齐（向前兼容）；review 的自动调整只改值、
    不改结构。create=False 供只读端点使用（不产生写副作用）。
    """
    rows = {c.key: c for c in DetectorDimConfig.objects.all()}
    out = []
    for spec in registry.DIMENSIONS:
        cfg = rows.get(spec['key'])
        if cfg is None and create and spec['enabled']:
            cfg = DetectorDimConfig.objects.create(
                key=spec['key'],
                name=spec['name'],
                weight=1.0,
                params=dict(spec['defaults']),
                is_enabled=True,
            )
        weight = cfg.weight if cfg else 1.0
        params = {**spec['defaults'], **((cfg.params or {}) if cfg else {})}
        enabled = cfg.is_enabled if cfg else spec['enabled']
        out.append({
            'key': spec['key'],
            'name': cfg.name if cfg and cfg.name else spec['name'],
            'weight': weight,
            'params': params,
            'targets': spec['targets'],
            'evaluate': spec['evaluate'],
            'enabled': enabled,
            'defaults': spec['defaults'],
            # 投票分组（#4）：注册表声明，未声明的维度自成方向组
            'group': spec.get('group') or spec['key'],
            'role': spec.get('role') or ROLE_DIRECTION,
            # 只展示/只作否决的维度不参加投票（det-v2）
            'vote_enabled': spec.get('vote_enabled', True),
        })
    return out


def _collect_targets():
    """对象宇宙：指数 + 自选股 + 板块。板块宇宙只依赖快照，快照缺就不含。

    板块快照每类只读一次（PERF-04）：宇宙取最近 _SECTOR_UNIVERSE_ROWS 个快照日
    的名字并集，同一份预加载数据挂在板块 target 的 'snapshots' 上，构建序列时
    直接复用，不再逐板块重读 45 天全量 JSON。
    """
    targets = []
    for code, name in TREND_INDICES:
        targets.append({'kind': 'index', 'code': code, 'name': name})
    for stock in Stock.objects.filter(is_active=True).order_by('code'):
        targets.append({'kind': 'stock', 'code': stock.code, 'name': stock.name, 'stock': stock,
                        'source': DetectorSignal.SOURCE_WATCHLIST})
    for kind in ('industry', 'concept'):
        snapshots = load_sector_snapshots(kind)
        names = set()
        for _trade_date, by_name in snapshots[-_SECTOR_UNIVERSE_ROWS:]:
            names.update(by_name)
        for name in sorted(names):
            targets.append({'kind': kind, 'code': name, 'name': name, 'snapshots': snapshots})
    return targets


def _build_context(target):
    """按对象类型构建序列上下文；返回 (ctx | None, err)。"""
    kind = target['kind']
    if kind == 'index':
        return build_index_context(target['code'], target['name'])
    if kind == 'stock':
        return build_stock_context(target['stock'])
    return build_sector_context(kind, target['code'], snapshots=target.get('snapshots'))


def _stale_reason(ctx, trade_date):
    """对象数据日期与扫描交易日不一致时返回降级原因，一致返回空串。"""
    as_of = ctx.get('as_of')
    try:
        as_of_date = signals._as_date(as_of) if as_of else None
    except (TypeError, ValueError):
        as_of_date = None
    if as_of_date is None:
        return '数据日期缺失，无法校验新鲜度'
    if as_of_date != trade_date:
        return f'数据过期：最新数据日 {as_of_date.isoformat()}，扫描交易日 {trade_date.isoformat()}'
    return ''


def _warm_review_sources():
    """回看前把指数日线拉进缓存，让 review_pending 在事务内不再触网。

    个股/板块的兑现判定只读本地表；指数回看要上游日线，相对基准超额要沪深300
    日线（与 review._index_outcome / _benchmark_bars 同一 days 参数即同一缓存键）。
    失败无妨：事务内会再试一次，指数如实跳过、超额留空。
    """
    from ..market import indices

    pending = DetectorSignal.objects.filter(
        status=DetectorSignal.STATUS_ACTIVE,
        review_hit__isnull=True,
    )
    codes = set(pending.filter(target_kind='index').values_list('target_code', flat=True))
    # 任何待回看信号都要算相对沪深300 的超额（#12），基准一并预热
    if codes or pending.exists():
        codes.add(review.BENCHMARK_CODE)
    for code in sorted(codes):
        try:
            indices.fetch_index_daily_series(code, days=130)
        except Exception as e:  # noqa: BLE001 上游边界：预热失败不影响扫描，取数时各维度自行降级
            logger.warning("回看指数日线预热失败 %s: %s", code, e)


def _clear_stale_signals(trade_date, cleared):
    """删除本轮确定无信号对象的当日未回看信号行（同日重跑幂等，ALG-08）。"""
    by_kind = {}
    for kind, code in cleared:
        by_kind.setdefault(kind, []).append(code)
    deleted = 0
    for kind, codes in by_kind.items():
        for i in range(0, len(codes), _CLEAR_BATCH):
            n, _ = DetectorSignal.objects.filter(
                trade_date=trade_date,
                review_hit__isnull=True,
                status__in=(DetectorSignal.STATUS_WATCHING, DetectorSignal.STATUS_ACTIVE),
                target_kind=kind,
                target_code__in=codes[i:i + _CLEAR_BATCH],
            ).delete()
            deleted += n
    return deleted


_SECTOR_KINDS = ('industry', 'concept')


def _null_vote_rate(cfg, kind=None):
    """零假设下维度的两侧合计投票率（只用于期望假阳估计，不参与判定）。"""
    by_kind = NULL_VOTE_RATES_BY_KIND.get(kind) or {}
    if cfg['key'] in by_kind:
        return by_kind[cfg['key']]
    if cfg['key'] in NULL_VOTE_RATES:
        return NULL_VOTE_RATES[cfg['key']]
    pct = (cfg.get('params') or {}).get('noise_pct', NOISE_DEFAULTS['noise_pct'])
    try:
        return min(1.0, max(0.0, 1.0 - float(pct))) or NULL_VOTE_RATE_FALLBACK
    except (TypeError, ValueError):
        return NULL_VOTE_RATE_FALLBACK


def null_fire_probability(configs, kind, code=None):
    """零假设下某类对象单日出信号的概率（维度独立假设下精确枚举）。

    每个适用维度以 p/2 投 up、p/2 投 down、1−p 不投（p 见 _null_vote_rate），
    枚举全部组合并调用与线上相同的 signals.vote 判定，累加出信号的概率。
    维度间实际正相关（同一条价格/量能序列），独立假设会低估；这里只给
    量级，用于「期望假阳 vs 实际信号数」的对照。
    """
    from itertools import product

    dims = [
        c for c in configs
        if c.get('enabled', True) and c.get('evaluate') is not None and kind in c['targets']
        and signals.dim_votes(c['key'], c)
        and not (c['key'] == 'margin_north' and code not in MARGIN_VOTE_CODES)
    ]
    if not dims:
        return 0.0
    rates = [_null_vote_rate(c, kind) for c in dims]
    prob = 0.0
    for combo in product((None, 'up', 'down'), repeat=len(dims)):
        p = 1.0
        results = {}
        for cfg, rate, d in zip(dims, rates, combo):
            p *= (1 - rate) if d is None else rate / 2
            group, role = signals.dim_group(cfg['key'], cfg)
            results[cfg['key']] = {'available': True, 'direction': d, 'value': 50.0,
                                   'weight': cfg.get('weight', 1.0), 'group': group, 'role': role}
        if p and signals.vote(results)['direction']:
            prob += p
    return prob


def multiple_testing_stats(configs, calm_targets, signal_count):
    """每次扫描的多重检验报告（#11）：期望假阳数 vs 实际信号数。

    calm_targets: [(kind, code)] 当日通过平静闸门、参与投票的对象。
    """
    cache = {}
    by_kind = {}
    for kind, code in calm_targets:
        sig = (kind, code if kind == 'index' and code in MARGIN_VOTE_CODES else None)
        if sig not in cache:
            cache[sig] = null_fire_probability(configs, kind, sig[1])
        by_kind[kind] = by_kind.get(kind, 0.0) + cache[sig]
    expected = sum(by_kind.values())
    return {
        'tested': len(calm_targets),
        'expected_false_positives': round(expected, 2),
        'expected_by_kind': {k: round(v, 2) for k, v in by_kind.items()},
        'signals': signal_count,
    }


def _sector_signal_cap():
    """每日板块信号上限：settings.DETECTOR_SECTOR_SIGNAL_CAP 覆盖默认值，0 = 不截断。"""
    try:
        return max(0, int(getattr(settings, 'DETECTOR_SECTOR_SIGNAL_CAP', SECTOR_SIGNAL_CAP_DEFAULT)))
    except (TypeError, ValueError):
        return SECTOR_SIGNAL_CAP_DEFAULT


def truncate_by_strength(collected, cap):
    """板块信号按强度降序只保留前 cap 个（#11）；返回 (保留, 截掉)。

    强度 = 得分 × 同侧加权票（signals.evaluate_target 的 'strength'），并列时
    维持中优先、再按对象 key 定序，保证同日重跑结果一致。指数/自选股不截。
    """
    sectors = [c for c in collected if c[1]['target_kind'] in _SECTOR_KINDS]
    if not cap or len(sectors) <= cap:
        return list(collected), []
    ranked = sorted(
        sectors,
        key=lambda c: (-(c[1].get('strength') or 0.0), -c[1]['cycles'], c[0]),
    )
    dropped_keys = {c[0] for c in ranked[cap:]}
    kept = [c for c in collected if c[0] not in dropped_keys]
    dropped = [c for c in collected if c[0] in dropped_keys]
    return kept, dropped


_LOAD_UNIVERSE = object()


def _xsection_params(configs):
    return {c['key']: c['params'] for c in configs
            if c.get('enabled', True) and c['key'] in _XSECTION_KEYS}


def _load_mb5_universe(params, trade_date):
    """全市场两融 MB5 截面（扫描一次读一次，成分池分块复用）；失败返回 None。"""
    if 'margin_crowding' not in params:
        return None
    try:
        return xsection.load_mb5_universe(
            trade_date, int(params['margin_crowding'].get('mb5_days', 5)))
    except Exception:  # noqa: BLE001 边界：两融截面读失败时 MB5 如实不可用
        logger.warning("全市场两融截面读取失败，MB5 本轮不可用", exc_info=True)
        return None


def _attach_cross_section(stock_ctxs, configs, universe=_LOAD_UNIVERSE, pool_ref=None):
    """给当日个股 ctx 挂上截面分位（xsection.attach_cross_section）；失败只降级截面维度。

    universe：预读的全市场 MB5 截面；缺省按 ctx 的 as_of 现读（单次调用）。
    pool_ref：成分池 DTC/ID20 参照（xsection.PoolReference）；None 时在 stock_ctxs 内取。
    """
    params = _xsection_params(configs)
    if not stock_ctxs or not params:
        return {}
    try:
        if universe is _LOAD_UNIVERSE:
            universe = None
            if 'margin_crowding' in params:
                # 全市场两融截面：T = 当日扫描日（新鲜度闸门保证各 ctx 的 as_of 都是 T）
                trade_date = max(str(c['as_of']) for c in stock_ctxs)
                universe = xsection.load_mb5_universe(
                    trade_date, int(params['margin_crowding'].get('mb5_days', 5)))
        return xsection.attach_cross_section(stock_ctxs, params, mb5_universe=universe,
                                             pool_ref=pool_ref)
    except Exception:  # noqa: BLE001 边界：截面计算失败时截面维度如实不可用，扫描照常
        logger.warning("截面特征计算失败，截面维度本轮不可用", exc_info=True)
        return {}


# ---------------------------------------------------------------------------
# 成分池（沪深300+中证500）
# ---------------------------------------------------------------------------

def _pool_setting(name, default, cast):
    try:
        return cast(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


def pool_signal_cap():
    """每日成分池推送上限：DETECTOR_POOL_SIGNAL_CAP 覆盖默认 20，0 = 不截断。"""
    return max(0, _pool_setting('DETECTOR_POOL_SIGNAL_CAP', POOL_SIGNAL_CAP_DEFAULT, int))


def pool_ready_ratio():
    return min(1.0, max(0.0, _pool_setting('DETECTOR_POOL_READY_RATIO', POOL_READY_RATIO_DEFAULT, float)))


def _pool_chunk():
    return max(10, _pool_setting('DETECTOR_POOL_CHUNK', POOL_CHUNK_DEFAULT, int))


def plan_pool(trade_date, watch_codes, mode='auto'):
    """决定本轮是否扫成分池；返回 {'status', 'codes', 'names', 'coverage'}。

    mode：auto=完整度 ≥ 阈值才扫；force=有多少扫多少（截止点）；skip=不扫。
    codes 只含当日日线已入库的成分股（自选股除外，它们按自选扫）。
    """
    from ..pool import collector
    from ..pool.constituents import pool_members

    off = {'status': DetectorRun.POOL_OFF, 'codes': [], 'names': {}, 'coverage': {}}
    if mode == 'skip' or not getattr(settings, 'DETECTOR_POOL_ENABLED', True):
        off['coverage'] = {'reason': '成分池扫描已关闭'}
        return off
    members = pool_members()
    if not members:
        off['coverage'] = {'reason': '成分表为空（未执行 refresh_index_constituents）'}
        return off
    names = {c: n for c, n in members.items() if c not in watch_codes}
    cov = collector.coverage(trade_date, codes=list(names))
    threshold = pool_ready_ratio()
    coverage = {
        'members': len(members),
        'watchlist_overlap': len(members) - len(names),
        'expected': cov['expected'],
        'fresh': cov['fresh'],
        'ratio': round(cov['ratio'], 4),
        'threshold': threshold,
        'missing_count': len(cov['missing']),
        'missing': cov['missing'][:100],
        'not_ready_count': len(cov['not_ready']),
        'not_ready': cov['not_ready'][:50],
        'mode': mode,
    }
    if mode == 'auto' and (not cov['expected'] or cov['ratio'] < threshold):
        return {'status': DetectorRun.POOL_PENDING, 'codes': [], 'names': names, 'coverage': coverage}
    fresh = [c for c in names if c not in set(cov['missing']) | set(cov['not_ready'])]
    return {'status': DetectorRun.POOL_DONE, 'codes': fresh, 'names': names, 'coverage': coverage}


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _load_float_histories(codes):
    """成分池个股的流通股本缓存 {code: [(date, 股数)] | None}（只读库，不触网）。"""
    from datetime import date as _date

    from ..models import PoolStockState

    out = {}
    for code, raw in PoolStockState.objects.filter(code__in=codes).values_list('code', 'float_shares'):
        try:
            out[code] = [(_date.fromisoformat(str(d)[:10]), float(v)) for d, v in (raw or [])] or None
        except (TypeError, ValueError):
            out[code] = None
    return out


def _iter_pool_contexts(codes, names, trade_date):
    """逐块产出 (target, target_key, ctx | None, err)；块内 Stock 与股本缓存一次查询。"""
    for chunk in _chunks(codes, _pool_chunk()):
        stocks = {s.code: s for s in Stock.objects.filter(code__in=chunk)}
        floats = _load_float_histories(chunk)
        for code in chunk:
            target = {'kind': 'stock', 'code': code, 'name': names.get(code) or code,
                      'source': DetectorSignal.SOURCE_POOL}
            key = f'stock:{code}'
            stock = stocks.get(code)
            if stock is None:
                yield target, key, None, '缺少 Stock 行'
                continue
            try:
                ctx, err = build_stock_context(stock, float_history=floats.get(code))
            except Exception as e:  # 边界：单只异常只跳过自己
                logger.warning("成分池序列构建异常 %s: %s", code, e, exc_info=True)
                ctx, err = None, f'序列构建异常: {e}'
            if ctx is not None:
                ctx['name'] = target['name']
                stale = _stale_reason(ctx, trade_date)
                if stale:
                    ctx, err = None, stale
            yield target, key, ctx, err


def truncate_pool_signals(collected, cap):
    """成分池信号按 严重度 → 向下优先 → 强度 只保留前 cap 条；返回 (保留, 截掉)。

    向上信号未经验证，同严重度下排在向下之后；自选/指数/板块不受影响。
    """
    pool = [c for c in collected if c[1].get('source') == DetectorSignal.SOURCE_POOL]
    if not cap or len(pool) <= cap:
        return list(collected), []
    sev = {DetectorSignal.SEVERITY_HIGH: 0, DetectorSignal.SEVERITY_MEDIUM: 1, DetectorSignal.SEVERITY_LOW: 2}
    ranked = sorted(pool, key=lambda c: (
        sev.get(c[1]['severity'], 3),
        c[1]['direction'] == DetectorSignal.DIRECTION_UP,
        -(c[1].get('strength') or 0.0),
        c[0],
    ))
    dropped_keys = {c[0] for c in ranked[cap:]}
    return ([c for c in collected if c[0] not in dropped_keys],
            [c for c in collected if c[0] in dropped_keys])


# 依赖扫描当日股票池截面的维度（det-v2）
_XSECTION_KEYS = ('margin_crowding', 'intraday_reversal')


def _sort_matrix_key(row):
    values = [abs(v) for v in row['values'].values() if v is not None]
    return max(values) if values else 0.0


def run_scan(trade_date=None, calendar=None, pool='auto'):
    """执行一次完整扫描；返回摘要 dict（也作为命令行输出）。

    calendar：可注入交易日历（带三态 is_trading_day(day) 的对象），透传给守卫、
    周期计数与回看；缺省走 stocks.trading_calendar（生产行为不变）。
    pool：成分池扫描方式（plan_pool）——auto 完整度达标才扫，force 有多少扫多少，skip 不扫。
    """
    trade_date = trade_date or timezone.localdate()

    # ---- 守卫（与快照落库同一时纪律）----
    if calendar is not None:
        is_trading_day = calendar.is_trading_day
    else:
        from ..trading_calendar import is_trading_day
    # 只在注入时透传，缺省调用与旧签名完全一致（测试替身不必认识 calendar）
    cal_kwargs = {'calendar': calendar} if calendar is not None else {}

    if not is_trading_day(trade_date):
        return {'skipped': 'non_trading_day', 'date': trade_date.isoformat()}
    now = timezone.localtime()
    if trade_date == now.date() and now.time() < dt_time(15, 30):
        return {'skipped': 'before_close', 'date': trade_date.isoformat()}

    degraded = []
    message_parts = []

    # ---- 融资余额快照（T+1 披露，探测器日度写入方）----
    try:
        margin_rows = save_margin_snapshot()
        if margin_rows:
            message_parts.append(f'融资余额快照新增 {margin_rows} 行')
    except Exception as e:  # noqa: BLE001  快照失败不阻断扫描，维度自行降级
        logger.warning("融资余额快照落库失败: %s", e)
        degraded.append({'target': 'snapshot:margin', 'reason': str(e)})

    configs = load_configs()
    targets = _collect_targets()
    matrix = {'industry': [], 'concept': []}
    counters = {'scanned': 0, 'calm': 0}
    # 循环里只收集，不落库：落库集中到循环后的事务里（ALG-08）
    collected = []  # [(target_key, signal_data, dim_results)]
    cleared = []  # 本轮确定无信号的 (kind, code)；降级对象不在其中
    calm_targets = []  # 参与投票的 (kind, code)，多重检验的分母（#11）
    vetoed = []  # 被修饰维度否决的信号（det-v2），写进 run.vetoed 供页面展示

    def evaluate(target, target_key, ctx):
        """单对象：平静判定 → 维度评估 → 收集信号；异常只降级自己（P0 ALG-01）。"""
        counters['scanned'] += 1
        try:
            is_calm, _meta = calm_mod.is_calm(ctx)
        except Exception as e:  # 边界：单对象异常只降级自己
            logger.exception("探测器平静判定异常 %s", target_key)
            degraded.append({'target': target_key, 'reason': f'平静判定异常: {e}'})
            return
        if not is_calm:
            cleared.append((target['kind'], target['code']))
            return
        counters['calm'] += 1

        try:
            signal_data, dim_results = signals.evaluate_target(ctx, configs, **cal_kwargs)
        except Exception as e:  # 边界：单对象异常只降级自己
            logger.exception("探测器维度评估异常 %s", target_key)
            degraded.append({'target': target_key, 'reason': f'维度评估异常: {e}'})
            return

        # 热力图矩阵：只收平静板块（平静前提不成立的板块不参与微变评估）
        if target['kind'] in matrix:
            matrix[target['kind']].append({
                'code': target['code'],
                'name': target['name'],
                'as_of': ctx['as_of'],
                'direction': signal_data['direction'] if signal_data else None,
                'score': signal_data['score'] if signal_data else 0.0,
                'values': {
                    key: (r.get('value') if r.get('available') else None)
                    for key, r in dim_results.items()
                },
            })

        calm_targets.append((target['kind'], target['code']))
        source = target.get('source') or DetectorSignal.SOURCE_MARKET
        if signal_data is None:
            record = signals.vetoed_record(dim_results)
            if record is not None:
                vetoed.append({**record, 'source': source})
            cleared.append((target['kind'], target['code']))
            return
        signal_data['source'] = source
        if signal_data.get('cycles_unknown'):
            degraded.append({'target': target_key, 'reason': '交易日历不可用，周期数无法确认，按周期 1 记'})
        collected.append((target_key, signal_data, dim_results))

    # 第一遍：构建序列并过新鲜度闸门。截面维度（det-v2 融资拥挤度等）要先看到
    # 当日全部个股，才能取截面分位，所以评估放到第二遍
    fresh = []  # [(target, target_key, ctx)]
    for target in targets:
        try:
            ctx, err = _build_context(target)
        except Exception as e:  # 边界：单对象异常只降级自己
            logger.warning("探测器序列构建异常 %s:%s: %s", target['kind'], target['code'], e, exc_info=True)
            ctx, err = None, f'序列构建异常: {e}'
        target_key = f"{target['kind']}:{target['code']}"
        if ctx is None:
            degraded.append({'target': target_key, 'reason': err})
            continue
        # 新鲜度闸门（ALG-11）：对象最新数据日必须就是扫描交易日。停牌、日线
        # 拉取持续失败或板块当日缺快照时，旧数据会以旧日期产出「今天」的信号、
        # 重复计数并立刻满足回看 cutoff——如实降级，不评估。
        stale_reason = _stale_reason(ctx, trade_date)
        if stale_reason:
            degraded.append({'target': target_key, 'reason': stale_reason})
            continue
        fresh.append((target, target_key, ctx))

    # ---- 成分池第一遍（分块）：只取 DTC/ID20 原始值组成截面参照，序列随即释放 ----
    watch_codes = {t['code'] for t in targets if t['kind'] == 'stock'}
    pool_plan = plan_pool(trade_date, watch_codes, mode=pool)
    xparams = _xsection_params(configs)
    pool_ref = None
    pool_fresh = []
    pool_skipped = []
    if pool_plan['status'] == DetectorRun.POOL_DONE:
        member_codes = set(pool_plan['names']) | (watch_codes & set(_pool_member_codes()))
        raw = {'dtc': {}, 'id20': {}}
        # 同在成分池的自选股：序列已在第一遍建好，直接取原始值
        for target, _key, ctx in fresh:
            if target['kind'] == 'stock' and target['code'] in member_codes:
                for feat, value in xsection.pool_features(ctx, xparams).items():
                    if value is not None:
                        raw[feat][target['code']] = value
        for target, _key, ctx, err in _iter_pool_contexts(pool_plan['codes'], pool_plan['names'], trade_date):
            if ctx is None:
                pool_skipped.append({'code': target['code'], 'reason': err})
                continue
            pool_fresh.append(target['code'])
            for feat, value in xsection.pool_features(ctx, xparams).items():
                if value is not None:
                    raw[feat][target['code']] = value
            del ctx
        pool_ref = xsection.PoolReference(raw)
        if pool_skipped:
            # 一条汇总进 degraded（不逐只写，避免挤掉其他降级记录）；明细在 pool_coverage
            degraded.append({'target': 'pool:skipped',
                             'reason': f'成分池 {len(pool_skipped)} 只序列不可用或数据过期，已跳过'})
    elif pool_plan['status'] == DetectorRun.POOL_PENDING:
        cov = pool_plan['coverage']
        message_parts.append(
            f"成分池日线完整度 {cov['fresh']}/{cov['expected']} 低于 {cov['threshold']:.0%}，本轮未扫成分池，等待补跑")

    # 截面分位（det-v2）：MB5 在全市场两融标的里取；DTC/ID20 在成分池当日日线里取
    # （成分池未扫时退回当日新鲜个股），样本不足 min_pool 不给分位
    universe = _load_mb5_universe(xparams, trade_date) if xparams else None
    pools = _attach_cross_section([ctx for t, _k, ctx in fresh if t['kind'] == 'stock'], configs,
                                  universe=universe, pool_ref=pool_ref)
    if pools:
        message_parts.append('截面样本 ' + '、'.join(f'{k}={v}' for k, v in sorted(pools.items())))

    for target, target_key, ctx in fresh:
        evaluate(target, target_key, ctx)
    del fresh

    # ---- 成分池第二遍（分块）：重建序列 → 挂截面分位 → 评估，块结束即释放 ----
    pool_scanned_before = counters['scanned']
    if pool_fresh:
        block = []
        for target, target_key, ctx, err in _iter_pool_contexts(pool_fresh, pool_plan['names'], trade_date):
            if ctx is None:
                pool_skipped.append({'code': target['code'], 'reason': err})
                continue
            block.append((target, target_key, ctx))
            if len(block) >= _pool_chunk():
                _evaluate_block(block, configs, universe, pool_ref, evaluate)
                block = []
        if block:
            _evaluate_block(block, configs, universe, pool_ref, evaluate)
        del block
    pool_scanned = counters['scanned'] - pool_scanned_before
    scanned, calm_count = counters['scanned'], counters['calm']
    new_count = sustained_count = 0

    # ---- 多重检验（#11）：板块按强度截断 + 期望假阳数 ----
    raw_signal_count = len(collected)
    raw_pool_count = sum(1 for c in collected if c[1].get('source') == DetectorSignal.SOURCE_POOL)
    collected, pool_dropped = truncate_pool_signals(collected, pool_signal_cap())
    if pool_dropped:
        for _key, signal_data, _dims in pool_dropped:
            cleared.append((signal_data['target_kind'], signal_data['target_code']))
        message_parts.append(f'成分池推送按严重度截断：保留前 {pool_signal_cap()} 条，截掉 {len(pool_dropped)} 条')
    pool_pushed = sum(1 for c in collected if c[1].get('source') == DetectorSignal.SOURCE_POOL)
    collected, dropped = truncate_by_strength(collected, _sector_signal_cap())
    if dropped:
        dropped_keys = set()
        for target_key, signal_data, _dims in dropped:
            # 截掉的对象按「本轮无信号」处理：同日重跑时旧行一并清理
            cleared.append((signal_data['target_kind'], signal_data['target_code']))
            dropped_keys.add((signal_data['target_kind'], signal_data['target_code']))
        for kind, rows in matrix.items():
            for row in rows:
                if (kind, row['code']) in dropped_keys:
                    row['truncated'] = True
        message_parts.append(f'板块信号按强度截断：保留前 {_sector_signal_cap()} 个，截掉 {len(dropped)} 个')
    mt_stats = multiple_testing_stats(configs, calm_targets, raw_signal_count)
    message_parts.append(
        f"期望假阳≈{mt_stats['expected_false_positives']:.1f}/实际 {raw_signal_count}"
        f"（检验 {mt_stats['tested']} 个平静对象）"
    )

    for board_rows in matrix.values():
        board_rows.sort(key=_sort_matrix_key, reverse=True)

    if not any(t['kind'] in matrix for t in targets) and targets:
        message_parts.append('板块快照缺失，本轮无板块对象')

    _warm_review_sources()

    # ---- 落库：信号 → 清理失效行 → 回看与调参 → run 行，同一事务（ALG-08）----
    with transaction.atomic():
        for target_key, signal_data, dim_results in collected:
            try:
                # 嵌套 atomic = savepoint：单条失败只回滚自己，外层事务可继续
                with transaction.atomic():
                    signals.upsert_signal(signal_data, dim_results)
            except Exception as e:  # 边界：单条落库失败只回滚自己的 savepoint
                logger.exception("探测器信号落库异常 %s", target_key)
                degraded.append({'target': target_key, 'reason': f'信号落库异常: {e}'})
                continue
            if signal_data['cycles'] >= 2:
                sustained_count += 1
            else:
                new_count += 1

        cleared_count = _clear_stale_signals(trade_date, cleared)
        if cleared_count:
            message_parts.append(f'重跑清除当日失效信号 {cleared_count} 条')

        review_summary = review.review_pending(
            trade_date, {c['key']: c for c in configs}, **cal_kwargs,
        )
        adjustments = review_summary.pop('adjustments', [])
        review_summary.pop('tune_stats', None)
        note = review_summary.pop('note', '')
        if note:
            message_parts.append(note)

        run, created = DetectorRun.objects.select_for_update().get_or_create(trade_date=trade_date)
        # 回看结果与调参流水是「当日累计」：重跑时已回看的行不会再被回看，
        # 覆盖写会把第一次的审计记录冲成空值
        prev_adjustments = [] if created else list(run.adjustments or [])
        prev_reviewed = 0 if created else run.reviewed_count
        prev_hits = 0 if created else run.hit_count
        run.reviewed_count = prev_reviewed + review_summary.get('reviewed', 0)
        run.hit_count = prev_hits + review_summary.get('hits', 0)
        run.hit_rate = (
            round(run.hit_count / run.reviewed_count, 3) if run.reviewed_count else None
        )
        run.adjustments = prev_adjustments + adjustments
        # 基线/超额/独立样本数按判定日从库里重算（#7），重跑不会重复累加
        for field, value in review.review_stats(trade_date).items():
            setattr(run, field, value)
        # 扫描侧字段反映最近一次运行的盘面，直接覆盖
        run.status = 'partial' if degraded else 'ok'
        run.targets_scanned = scanned
        run.calm_count = calm_count
        run.signals_new = new_count
        run.signals_sustained = sustained_count
        run.degraded = degraded[:200]  # 上限防异常日把 run 行撑爆
        run.matrix = matrix
        # 否决记录：自选/指数/板块在前，成分池只留前若干条（成分池同日可能有几十条）
        vetoed.sort(key=lambda r: r.get('source') == DetectorSignal.SOURCE_POOL)
        run.vetoed = vetoed[:200]
        run.message = '；'.join(message_parts)[:300]
        # 成分池：pending 不覆盖已 done 的同日记录（补跑后 17:xx 的重跑不会把状态打回去）
        if pool_plan['status'] != DetectorRun.POOL_PENDING or run.pool_status != DetectorRun.POOL_DONE:
            run.pool_status = pool_plan['status']
            run.pool_coverage = {
                **pool_plan['coverage'],
                'scanned': pool_scanned,
                'skipped_count': len(pool_skipped),
                'skipped': pool_skipped[:100],
                'signals_raw': raw_pool_count,
                'signals_pushed': pool_pushed,
                'cap': pool_signal_cap(),
            }
        run.save()
    _cleanup(trade_date)

    summary = {
        'date': trade_date.isoformat(),
        'status': run.status,
        'targets_scanned': scanned,
        'calm_count': calm_count,
        'signals_new': new_count,
        'signals_sustained': sustained_count,
        'reviewed': review_summary.get('reviewed', 0),
        'hits': review_summary.get('hits', 0),
        'hit_rate': review_summary.get('hit_rate'),
        # 同标的无条件基线命中率（ALG-05）：hit_rate 要和它比才有意义
        'baseline_rate': review_summary.get('baseline_rate'),
        'excess_rate': run.excess_rate,
        'independent_samples': run.independent_samples,
        'degraded_count': len(degraded),
        'adjustments': len(adjustments),
        'vetoed': len(vetoed),
        # 多重检验报告（#11）：DetectorRun 暂无专门字段，摘要见 run.message
        'multiple_testing': {**mt_stats, 'truncated': len(dropped)},
        'pool': {
            'status': pool_plan['status'],
            'ratio': pool_plan['coverage'].get('ratio'),
            'scanned': pool_scanned,
            'skipped': len(pool_skipped),
            'signals_raw': raw_pool_count,
            'signals_pushed': pool_pushed,
        },
    }
    logger.info("探测器扫描完成: %s", summary)
    return summary


def _pool_member_codes():
    from ..pool.constituents import pool_members

    return pool_members().keys()


def _evaluate_block(block, configs, universe, pool_ref, evaluate):
    """成分池一块：挂截面分位后逐只评估。"""
    _attach_cross_section([ctx for _t, _k, ctx in block], configs, universe=universe, pool_ref=pool_ref)
    for target, target_key, ctx in block:
        evaluate(target, target_key, ctx)


def _cleanup(trade_date):
    """按保留策略清理旧数据（run 含热力图矩阵较大，保留更短）。"""
    signal_days = int(getattr(settings, 'DETECTOR_SIGNAL_RETENTION_DAYS', 600))
    run_days = int(getattr(settings, 'DETECTOR_RUN_RETENTION_DAYS', 120))
    deleted_sig, _ = DetectorSignal.objects.filter(
        trade_date__lt=trade_date - timedelta(days=signal_days)
    ).delete()
    deleted_run, _ = DetectorRun.objects.filter(
        trade_date__lt=trade_date - timedelta(days=run_days)
    ).delete()
    if deleted_sig or deleted_run:
        logger.info("探测器清理: 信号 %s 行 / 运行记录 %s 行", deleted_sig, deleted_run)
