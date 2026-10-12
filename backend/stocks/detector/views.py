"""探测器 REST 视图：信号列表 / 信号详情 / 板块微变热力图 / 维度配置。

对齐项目既有规范：
- @api_view 函数视图 + 统一 meta（available/source/data_as_of/cache_status/
  disclaimer），前端 MarketDataStatus 组件直接可读；
- 上游/快照不足不报 500，而是在 meta 与 message 里如实降级；
- 分页复用 common.pagination.paginate（page/page_size，默认 50 上限 100）。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from ..common.envelope import envelope
from ..common.pagination import paginate
from ..common.params import parse_bool, parse_date, positive_int
from ..trading_calendar import expected_latest_date
from ..models import DetectorDimConfig, DetectorRun, DetectorSignal, DetectorTuneLog
from . import constants, registry
from .dimensions import (
    build_index_context,
    build_sector_context,
    build_stock_context,
)
from .scan import _sector_signal_cap
from .signals import dim_group, dim_votes

logger = logging.getLogger(__name__)

_META_SOURCE = '本站探测器日度扫描（上游：东财/同花顺/新浪/沪深交易所公开数据，经日度快照积累）'
_META_DISCLAIMER_TAIL = '历史命中不代表未来表现。委比挂单、板块内分化两维度因无数据源暂未启用。'


def _meta_disclaimer():
    """免责声明随自动调参开关变化（#5）：默认关闭时不能再说「自动微调」。"""
    if getattr(settings, 'DETECTOR_AUTO_TUNE', False):
        tune = '维度权重与噪声闸门按跨日累计、显著跑赢/跑输基线才有界调整，'
    else:
        tune = '维度参数固定（在线自动调参已关闭，复盘只统计不调参），'
    return f'信号为盘面形态识别，非买卖建议；{tune}{_META_DISCLAIMER_TAIL}'


# 信号列表已上线的 JSON 键顺序，接入 common.envelope 时保持不变（ARC-04）
_SIGNALS_KEY_ORDER = ('meta', 'trade_date', 'latest_run', 'summary', 'pagination', 'items')

# DetectorRun 里的大 JSON 字段：列表/元信息只用标量，读这些字段纯属浪费（PERF-09）
_HEAVY_RUN_FIELDS = ('matrix', 'degraded', 'adjustments')


def _runs_brief():
    """只取标量字段的 DetectorRun 查询（meta/摘要用），按交易日倒序。"""
    return DetectorRun.objects.defer(*_HEAVY_RUN_FIELDS).order_by('-trade_date')


def _meta(latest_run, message=''):
    """探测器响应的统一 meta；fresh = 最近一次 run 覆盖了最近已完成交易日。"""
    cache_status = 'unavailable'
    data_date = None
    fetched_at = None
    if latest_run is not None:
        cache_status = 'stale'
        data_date = latest_run.trade_date.isoformat()
        fetched_at = latest_run.updated_at.isoformat(timespec='seconds')
        try:
            expected = expected_latest_date()
            if expected and latest_run.trade_date >= expected:
                cache_status = 'fresh'
        except Exception:
            logger.warning("探测器 meta 时效判定失败，按 stale 处理", exc_info=True)
    return {
        'available': latest_run is not None and latest_run.status != 'failed',
        'source': _META_SOURCE,
        'source_data_date': data_date,
        'data_as_of': data_date,
        'fetched_at': fetched_at,
        'cache_status': cache_status,
        'disclaimer': _meta_disclaimer(),
        **({'message': message} if message else {}),
    }


def _signal_row(sig):
    return {
        'id': sig.id,
        'target_kind': sig.target_kind,
        'target_kind_display': sig.get_target_kind_display(),
        'target_code': sig.target_code,
        'target_name': sig.target_name,
        'trade_date': sig.trade_date,
        'direction': sig.direction,
        'cycles': sig.cycles,
        'score': sig.score,
        'severity': sig.severity,
        'status': sig.status,
        'status_display': sig.get_status_display(),
        'dimensions': sig.dimensions,
        'review_hit': sig.review_hit,
        'hit_date': sig.hit_date,
        'reviewed_at': sig.reviewed_at,
        # 回看明细（#12）：v1 口径的旧行全部为 None / ''
        'review_version': sig.review_version,
        'review_threshold': sig.review_threshold,
        'review_baseline': sig.review_baseline,
        'fwd_return': sig.fwd_return,
        'bench_return': sig.bench_return,
        'excess_return': sig.excess_return,
        'benchmark_code': sig.benchmark_code,
        'created_at': sig.created_at,
    }


@api_view(['GET'])
def detector_signals(request):
    """信号列表：默认返回最近一次扫描交易日的信号。

    筛选：severity(high/medium/low)、direction(up/down)、
    target_kind(index/stock/industry/concept)、status、date=YYYY-MM-DD、
    all_dates=1（近 30 天历史，默认只看最新扫描日）。
    """
    latest_run = _runs_brief().first()

    # 只认小写 '1'/'true'，与改用 common/params 之前一致
    all_dates = parse_bool(request.query_params.get('all_dates'), truthy=('1', 'true'), ignore_case=False)
    date_raw = request.query_params.get('date')
    trade_date = None
    if date_raw:
        trade_date = parse_date(date_raw)
        if trade_date is None:
            return Response({'detail': 'date 格式应为 YYYY-MM-DD'},
                            status=status.HTTP_400_BAD_REQUEST)

    qs = DetectorSignal.objects.all()
    if trade_date is not None:
        qs = qs.filter(trade_date=trade_date)
    elif all_dates:
        # 历史视图固定 30 天窗：防全表倒出（SQLite 单机 + 1H2G 的习惯约束）
        # 只在这里加日期窗；300 行上限的切片放到所有 filter 之后（先切片再 filter 会 500，P0 ALG-03）
        qs = qs.filter(trade_date__gte=timezone.localdate() - timedelta(days=30))
    elif latest_run is not None:
        qs = qs.filter(trade_date=latest_run.trade_date)
    else:
        return Response({
            'meta': _meta(None, '探测器尚未运行过扫描（每交易日 17:15 自动执行）'),
            'summary': {'total': 0},
            'items': [],
        })

    for field, choices in (
        ('severity', dict(DetectorSignal.SEVERITY_CHOICES)),
        ('direction', dict(DetectorSignal.DIRECTION_CHOICES)),
        ('target_kind', dict(DetectorSignal.TARGET_KIND_CHOICES)),
        ('status', dict(DetectorSignal.STATUS_CHOICES)),
    ):
        value = request.query_params.get(field)
        if value:
            if value not in choices:
                return Response(
                    {'detail': f'{field} 不支持，可选：{", ".join(choices)}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            qs = qs.filter(**{field: value})

    if all_dates and trade_date is None:
        qs = qs.order_by('-trade_date')[:300]
    rows = [_signal_row(s) for s in qs]
    if not all_dates and trade_date is None and latest_run is not None:
        rows.sort(key=lambda r: r['score'], reverse=True)
    items, pagination = paginate(
        rows,
        positive_int(request.query_params, 'page', 1),
        # page_size 小于 1 钳到 1（不是回退 50），上限 100：保持接入前的口径
        positive_int(request.query_params, 'page_size', 50, 100, clamp=True),
    )

    summary = {
        'total': len(rows),
        'by_severity': {},
        'by_direction': {},
        'by_status': {},
    }
    for row in rows:
        summary['by_severity'][row['severity']] = summary['by_severity'].get(row['severity'], 0) + 1
        summary['by_direction'][row['direction']] = summary['by_direction'].get(row['direction'], 0) + 1
        summary['by_status'][row['status']] = summary['by_status'].get(row['status'], 0) + 1

    return Response(envelope(
        items, pagination, _meta(latest_run),
        key_order=_SIGNALS_KEY_ORDER,
        trade_date=trade_date.isoformat() if trade_date else (latest_run.trade_date.isoformat() if latest_run else None),
        latest_run=_run_brief(latest_run) if latest_run else None,
        summary=summary,
    ))


def _run_brief(run):
    return {
        'trade_date': run.trade_date,
        'status': run.status,
        'targets_scanned': run.targets_scanned,
        'calm_count': run.calm_count,
        'signals_new': run.signals_new,
        'signals_sustained': run.signals_sustained,
        'reviewed_count': run.reviewed_count,
        'hit_count': run.hit_count,
        'hit_rate': run.hit_rate,
        # 命中率的参照系（#7）：旧 run 行为 None，前端显示「-」
        'baseline_rate': run.baseline_rate,
        'excess_rate': run.excess_rate,
        'independent_samples': run.independent_samples,
        'avg_excess_return': run.avg_excess_return,
        'message': run.message,
    }


def _context_for_signal(sig):
    """为信号重建序列上下文（详情页曲线用）；失败返回 (None, err)。"""
    if sig.target_kind == DetectorSignal.TARGET_INDEX:
        return build_index_context(sig.target_code, sig.target_name)
    if sig.target_kind == DetectorSignal.TARGET_STOCK:
        from ..models import Stock

        stock = Stock.objects.filter(code=sig.target_code).first()
        if stock is None:
            return None, '该股票已不在自选库，无法重建序列'
        return build_stock_context(stock)
    return build_sector_context(sig.target_kind, sig.target_code)


@api_view(['GET'])
def detector_signal_detail(request, pk):
    """信号详情：落库的维度判定 + 各维度近 20 日曲线（评估时顺带重算）。

    曲线在详情请求时按当前序列重算——信号行的 dimensions 是信号当日快照
    （不可变事实），曲线是活数据（可以看到信号之后各维度怎么走）。
    重算用信号当日的参数快照（ALG-15）；旧信号行没有快照时退回当前参数，
    params_source 标明用的是哪一份。
    重算失败（上游断/快照过期）时如实返回已落库部分并标注。
    """
    sig = DetectorSignal.objects.filter(pk=pk).first()
    if sig is None:
        return Response({'detail': '信号不存在'}, status=status.HTTP_404_NOT_FOUND)

    ctx, err = _context_for_signal(sig)
    dimension_series = {}
    params_source = {}
    if ctx is not None:
        cfg_rows = {c.key: c for c in DetectorDimConfig.objects.all()}
        for key, stored in (sig.dimensions or {}).items():
            spec = registry.DIMENSION_MAP.get(key)
            if spec is None or spec['evaluate'] is None:
                dimension_series[key] = []
                continue
            snapshot = (stored or {}).get('params')
            if snapshot:
                params = {**spec['defaults'], **snapshot}
                params_source[key] = 'signal'
            else:
                cfg = cfg_rows.get(key)
                params = {**spec['defaults'], **((cfg.params or {}) if cfg else {})}
                params_source[key] = 'current'
            try:
                res = spec['evaluate'](ctx, params)
                dimension_series[key] = res['series']
            except Exception:  # 边界：曲线重算失败不影响已落库明细
                logger.warning("探测器维度 %s 曲线重算失败", key, exc_info=True)
                dimension_series[key] = []

    return Response({
        'meta': _meta(_runs_brief().first(),
                      message=(err or '') if ctx is None else ''),
        'signal': _signal_row(sig),
        'dimension_series': dimension_series,
        'params_source': params_source,
        'disabled_dims': [
            {'key': d['key'], 'name': d['name'], 'disabled_reason': d['disabled_reason'],
             'disabled_kind': _disabled_kind(d, False)}
            for d in registry.DIMENSIONS if not d['enabled']
        ],
    })


@api_view(['GET'])
def detector_heatmap(request):
    """板块微变热力图：最近一次扫描的平静板块 × 维度微变分值矩阵。

    ?board=industry|concept（默认 industry）；行按最大 |分值| 降序并截取
    前 40 行（热力图可读性上限，完整数据仍在 run.matrix 里）。
    """
    board = request.query_params.get('board', DetectorSignal.TARGET_INDUSTRY)
    if board not in (DetectorSignal.TARGET_INDUSTRY, DetectorSignal.TARGET_CONCEPT):
        return Response({'detail': 'board 仅支持 industry / concept'},
                        status=status.HTTP_400_BAD_REQUEST)

    run = None
    rows = []
    # 只要矩阵与 meta 用到的标量；degraded/adjustments 不读
    for candidate in DetectorRun.objects.defer('degraded', 'adjustments').order_by('-trade_date')[:5]:
        rows = (candidate.matrix or {}).get(board) or []
        if rows:
            run = candidate
            break

    if run is None:
        return Response({
            'meta': _meta(None, '暂无板块微变矩阵（等待首个交易日扫描完成，或板块快照尚未积累）'),
            'board': board,
            'dims': [],
            'rows': [],
        })

    # 热力图只列投票维度：只展示/只作否决的维度（det-v2）不评估板块
    keys = [k for k in registry.ENABLED_KEYS if dim_votes(k)]
    dim_names = {d['key']: d['name'] for d in registry.DIMENSIONS if d['enabled']}
    dims = [{'key': k, 'name': dim_names[k]} for k in keys]
    shown = [
        {
            'code': r.get('code'),
            'name': r.get('name'),
            'as_of': r.get('as_of'),
            'direction': r.get('direction'),
            'score': r.get('score'),
            'values': {k: r.get('values', {}).get(k) for k in keys},
        }
        for r in rows[:40]
    ]
    return Response({
        'meta': _meta(run),
        'board': board,
        'dims': dims,
        'rows': shown,
    })


def _disabled_kind(spec, enabled):
    """停用原因的类别：candidate=已实现但回测未证明有效的候选维度；
    no_source=本站没有数据源/尚未实现；启用中为空串。"""
    if enabled:
        return ''
    return 'no_source' if spec.get('evaluate') is None else 'candidate'


def _vote_groups():
    """投票信息组（#4）：{group: {'role', 'dims': [启用维度 key...]}}，与 signals.vote 同源。"""
    groups = {}
    for spec in registry.DIMENSIONS:
        if not spec['enabled'] or not dim_votes(spec['key']):
            continue
        group, role = dim_group(spec['key'])
        entry = groups.setdefault(group, {'role': role, 'dims': []})
        entry['dims'].append(spec['key'])
    return groups


@api_view(['GET'])
def detector_config(request):
    """维度配置与迭代状态（只读）：权重/阈值/启停原因 + 近 10 次运行摘要 + 调参流水。"""
    cfg_rows = {c.key: c for c in DetectorDimConfig.objects.all()}
    dims = []
    for spec in registry.DIMENSIONS:
        cfg = cfg_rows.get(spec['key'])
        enabled = cfg.is_enabled if cfg else spec['enabled']
        group, role = dim_group(spec['key'])
        dims.append({
            'key': spec['key'],
            'name': cfg.name if cfg and cfg.name else spec['name'],
            'enabled': enabled,
            'disabled_reason': (cfg.disabled_reason if cfg else None) or spec.get('disabled_reason', ''),
            'disabled_kind': _disabled_kind(spec, enabled),
            # 适用对象与投票分组（前端「适用对象」列与计票说明直接用，不再手写映射）
            'targets': list(spec['targets']),
            'vote_codes': list(constants.MARGIN_VOTE_CODES) if spec['key'] == 'margin_north' else None,
            'group': group,
            'role': role,
            # False = 只展示/只作否决，不参加投票（det-v2）
            'vote_enabled': dim_votes(spec['key']),
            'note': spec['note'],
            'weight': cfg.weight if cfg else 1.0,
            'params': {**spec['defaults'], **((cfg.params or {}) if cfg else {})},
            'defaults': spec['defaults'],
        })
    run_rows = list(_runs_brief()[:10])
    runs = [_run_brief(r) for r in run_rows]
    return Response({
        'meta': _meta(run_rows[0] if run_rows else None),
        'dims': dims,
        'runs': runs,
        # 规则直接引用 detector/constants 里的常量（ALG-15 / ARC-06）：实现与展示
        # 共用一份，旧实现在这里手写了一份副本，改阈值后配置页会与实际不一致
        'review_rules': {
            'version': constants.REVIEW_VERSION,
            'lookback_days': constants.REVIEW_LOOKBACK,
            'hit_sigma_k': getattr(settings, 'DETECTOR_REVIEW_SIGMA_K', constants.HIT_SIGMA_K),
            'sigma_window': constants.SIGMA_WINDOW,
            'hit_pct': constants.HIT_PCT,
            'benchmark': constants.BENCHMARK_CODE,
            # 基期：指数/个股 T+1 开盘；板块无开盘价，T+1 收盘
            'base': {'index': 'open_t1', 'stock': 'open_t1', 'industry': 'close_t1', 'concept': 'close_t1'},
            'auto_tune': bool(getattr(settings, 'DETECTOR_AUTO_TUNE', False)),
            'tune_window_days': getattr(settings, 'DETECTOR_TUNE_WINDOW_DAYS', constants.TUNE_WINDOW_DAYS),
            'confidence': constants.TUNE_CONFIDENCE,
            'prior_n': constants.TUNE_PRIOR_N,
            'weight_bounds': list(constants.WEIGHT_BOUNDS),
            'tune_params': {k: list(v['bounds']) for k, v in constants.TUNE_PARAMS.items()},
            'min_samples': constants.MIN_ADJ_SAMPLES,
        },
        # 调参流水（#16，独立表、永久保留）：最近 20 条，含关闭时的演练记录
        'tune_log': [
            {
                'trade_date': t.trade_date,
                'dim_key': t.dim_key,
                'source': t.source,
                'applied': t.applied,
                'changes': t.changes,
                'stats': t.stats,
                'reason': t.reason,
            }
            for t in DetectorTuneLog.objects.order_by('-trade_date', '-id')[:20]
        ],
        # 平静闸门与分级参数只读展示（不参与自动调参，理由见 calm.py 文件尾）
        'calm_rules': {
            'price': dict(constants.PRICE_CALM_PARAMS),
            'sector': dict(constants.SECTOR_CALM_PARAMS),
        },
        'vote_rules': {
            'min_dims': constants.VOTE_MIN_DIMS,
            # 分组加权投票（#4）：同侧加权票下限、对侧比例、信息组构成
            'min_weight': constants.VOTE_MIN_WEIGHT,
            'oppose_ratio': constants.OPPOSE_RATIO,
            'groups': _vote_groups(),
            'severity': dict(constants.SEVERITY_RULES),
            # 多重检验（#11）：每日板块信号按强度截断的上限，0 = 不截断
            'sector_signal_cap': _sector_signal_cap(),
        },
    })
