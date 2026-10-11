"""探测器扫描编排：一个交易日一次完整扫描，产出信号、热力图矩阵与运行日志。

流程（每交易日 17:15 由调度器触发，也可用 run_detector_scan 命令手动执行）：
1. 守卫：非交易日 / 未收盘（15:30 前）直接跳过——与快照落库同一时纪律，
   防止把上一交易日的盘面标成今天（2026-09-07 踩过的坑）；
2. 融资余额快照落库（两融 T+1，探测器是它唯一的日度写入方）；
3. 收集对象宇宙：5 个主要指数 + 全部自选股 + 最近快照出现过的行业/概念板块；
4. 逐对象：构建序列 → 平静判定（不满足直接跳过）→ 维度评估 → 投票落库；
5. 回看 5 个交易日前信号并按命中率微调权重/阈值（review.py）；
6. upsert DetectorRun（矩阵、降级、调参流水），按保留策略清理旧数据。

幂等性：信号行与 run 行都按日期 update_or_create，重跑覆盖当日结果，
不会产生重复信号或双重调参（回看只处理 review_hit 仍为 null 的行）。
"""

from __future__ import annotations

import logging
from datetime import time as dt_time, timedelta

from django.conf import settings
from django.utils import timezone

from ..market.indices import TREND_INDICES
from ..market.snapshots import save_margin_snapshot
from ..models import DetectorDimConfig, DetectorRun, DetectorSignal, MarketDailySnapshot, Stock
from . import calm as calm_mod
from . import registry, review, signals
from .dimensions import build_index_context, build_sector_context, build_stock_context

logger = logging.getLogger(__name__)

# 板块宇宙取最近 3 个快照日的名字并集：某板块缺最近一天时不至于整个
# 掉出宇宙（其序列会带 None，由维度评估如实降级），宇宙本身保持稳定。
_SECTOR_UNIVERSE_ROWS = 3


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
        })
    return out


def _collect_targets():
    """对象宇宙：指数 + 自选股 + 板块。板块宇宙只依赖快照，快照缺就不含。"""
    targets = []
    for code, name in TREND_INDICES:
        targets.append({'kind': 'index', 'code': code, 'name': name})
    for stock in Stock.objects.filter(is_active=True).order_by('code'):
        targets.append({'kind': 'stock', 'code': stock.code, 'name': stock.name, 'stock': stock})
    for kind_db, kind in (
        (MarketDailySnapshot.KIND_INDUSTRY_FF, 'industry'),
        (MarketDailySnapshot.KIND_CONCEPT_FF, 'concept'),
    ):
        names = set()
        rows = MarketDailySnapshot.objects.filter(kind=kind_db).order_by('-trade_date')[
            :_SECTOR_UNIVERSE_ROWS
        ]
        for snap in rows:
            for row in snap.payload or []:
                if row.get('name'):
                    names.add(row['name'])
        for name in sorted(names):
            targets.append({'kind': kind, 'code': name, 'name': name})
    return targets


def _build_context(target):
    """按对象类型构建序列上下文；返回 (ctx | None, err)。"""
    kind = target['kind']
    if kind == 'index':
        return build_index_context(target['code'], target['name'])
    if kind == 'stock':
        return build_stock_context(target['stock'])
    return build_sector_context(kind, target['code'])


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


def _sort_matrix_key(row):
    values = [abs(v) for v in row['values'].values() if v is not None]
    return max(values) if values else 0.0


def run_scan(trade_date=None):
    """执行一次完整扫描；返回摘要 dict（也作为命令行输出）。"""
    trade_date = trade_date or timezone.localdate()

    # ---- 守卫（与快照落库同一时纪律）----
    from ..services import is_trading_day

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
        logger.warning(f'融资余额快照落库失败: {e}')
        degraded.append({'target': 'snapshot:margin', 'reason': str(e)})

    configs = load_configs()
    targets = _collect_targets()
    matrix = {'industry': [], 'concept': []}
    scanned = calm_count = new_count = sustained_count = 0

    for target in targets:
        try:
            ctx, err = _build_context(target)
        except Exception as e:  # noqa: BLE001  单对象异常只降级自己
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

        scanned += 1
        # 平静判定与维度评估同样按单对象隔离：一个对象的脏序列不能让整轮扫描崩溃（P0 ALG-01）
        try:
            is_calm, _meta = calm_mod.is_calm(ctx)
        except Exception as e:  # noqa: BLE001
            logger.exception(f'探测器平静判定异常 {target_key}')
            degraded.append({'target': target_key, 'reason': f'平静判定异常: {e}'})
            continue
        if not is_calm:
            continue
        calm_count += 1

        try:
            signal_data, dim_results = signals.evaluate_target(ctx, configs)
        except Exception as e:  # noqa: BLE001
            logger.exception(f'探测器维度评估异常 {target_key}')
            degraded.append({'target': target_key, 'reason': f'维度评估异常: {e}'})
            continue

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

        if signal_data is None:
            continue
        try:
            signals.upsert_signal(signal_data, dim_results)
        except Exception as e:  # noqa: BLE001
            logger.exception(f'探测器信号落库异常 {target_key}')
            degraded.append({'target': target_key, 'reason': f'信号落库异常: {e}'})
            continue
        if signal_data['cycles'] >= 2:
            sustained_count += 1
        else:
            new_count += 1

    for board_rows in matrix.values():
        board_rows.sort(key=_sort_matrix_key, reverse=True)

    # ---- 回看与调参 ----
    review_summary = review.review_pending(trade_date, {c['key']: c for c in configs})
    adjustments = review_summary.pop('adjustments', [])
    note = review_summary.pop('note', '')

    if not any(t['kind'] in matrix for t in targets) and targets:
        message_parts.append('板块快照缺失，本轮无板块对象')
    if note:
        message_parts.append(note)

    run, _created = DetectorRun.objects.update_or_create(
        trade_date=trade_date,
        defaults={
            'status': 'partial' if degraded else 'ok',
            'targets_scanned': scanned,
            'calm_count': calm_count,
            'signals_new': new_count,
            'signals_sustained': sustained_count,
            'reviewed_count': review_summary.get('reviewed', 0),
            'hit_count': review_summary.get('hits', 0),
            'hit_rate': review_summary.get('hit_rate'),
            'degraded': degraded[:200],  # 上限防异常日把 run 行撑爆
            'adjustments': adjustments,
            'matrix': matrix,
            'message': '；'.join(message_parts),
        },
    )
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
        'degraded_count': len(degraded),
        'adjustments': len(adjustments),
    }
    logger.info(f'探测器扫描完成: {summary}')
    return summary


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
        logger.info(f'探测器清理: 信号 {deleted_sig} 行 / 运行记录 {deleted_run} 行')
