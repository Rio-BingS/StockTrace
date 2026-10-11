"""市场接口：同步拉上游，带总时限与熔断（REL-05，工具见 common/upstream）。"""

import logging
import uuid
from datetime import date

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from ..common.params import positive_int
from ..common.upstream import (
    _BREAKER_OPEN_SECONDS,
    UpstreamCircuitOpen,
    UpstreamTimeout,
    _call_with_deadline,
    _request_deadline,
)
from ..market import (
    fetch_stock_margin,
    get_etf_detail,
    get_etf_share_radar,
    get_institution_holdings,
    get_market_fund_flow_window,
    get_market_overview,
    get_market_trend,
    get_national_team_etfs,
    get_national_team_flow,
    get_northbound_window,
    get_sector_rotation,
)
from ..market.periods import FULL_PRESETS, resolve_period
from ..serializers import normalize_stock_code

logger = logging.getLogger(__name__)


def _deadline_error_response(exc, label):
    if isinstance(exc, UpstreamCircuitOpen):
        return Response(
            {'detail': f'{label}上游连续超时，暂停访问 {_BREAKER_OPEN_SECONDS} 秒后再试'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    logger.warning(f'获取{label}超过 {_request_deadline():.0f}s 总时限')
    return Response({'detail': f'获取{label}超时，请稍后重试'}, status=status.HTTP_504_GATEWAY_TIMEOUT)


def _market_response(loader, label):
    try:
        return Response(_call_with_deadline(loader, label))
    except (UpstreamTimeout, UpstreamCircuitOpen) as e:
        return _deadline_error_response(e, label)
    except Exception:
        return _upstream_failed(label)


def _upstream_failed(label):
    """上游/内部异常的统一出口：对外只给通用文案与 error_id，详情进日志（SEC-07）。

    异常原文可能含上游响应片段、内网地址或文件路径，不回显给客户端；
    error_id 同时写进日志，排障时按它在日志里定位堆栈。
    """
    error_id = uuid.uuid4().hex[:12]
    logger.exception(f'获取{label}失败 error_id={error_id}')
    return Response(
        {'detail': f'获取{label}失败，请稍后重试（错误编号 {error_id}）',
         'error_id': error_id},
        status=status.HTTP_502_BAD_GATEWAY,
    )


@api_view(['GET'])
def market_overview(request):
    """大盘首页：主要指数 + 资金/情绪摘要 + 二级模块入口"""
    return _market_response(get_market_overview, '大盘数据')


@api_view(['GET'])
def market_trend(request):
    """全市场走势：多指数归一化对比。

    区间三选一：?period=1w|1m|3m|6m|1y|ytd（默认 3m）、
    ?days=30|60|120|250（兼容旧参数）、?start=&end=（自定义，YYYY-MM-DD）。
    """
    def load():
        params = request.query_params
        # 优先级沿用旧实现：start/end 自定义 > days 旧参数 > period 档位
        if params.get('days') and not (params.get('start') or params.get('end')):
            days = _positive_int(request, 'days', 120)
            if days not in (30, 60, 120, 250):
                raise ValueError('days 仅支持 30、60、120、250')
            return get_market_trend(days=days)
        window = resolve_period(
            params.get, list(_TREND_PERIOD_DAYS) + ['ytd'], default='3m',
            max_days=1100, custom_default_days=90, messages=_TREND_PERIOD_MESSAGES,
        )
        if window.preset == 'custom':
            return get_market_trend(start=window.start.isoformat(), end=window.end.isoformat())
        if window.preset == 'ytd':
            # 自然日折算交易日（×5/7），口径同旧实现
            today = window.end
            days = max(5, int((today - date(today.year, 1, 1)).days * 5 / 7))
        else:
            days = _TREND_PERIOD_DAYS[window.preset]
        return get_market_trend(days=min(days, 300))
    return _market_bad_request(load, '全市场走势')


# 走势档位按交易日计（与 periods 的自然日档位不同）；400 文案保持旧版，前端直接展示
_TREND_PERIOD_DAYS = {'1w': 5, '1m': 22, '3m': 66, '6m': 130, '1y': 260}
_TREND_PERIOD_MESSAGES = {
    'bad_period': 'period 必须是 1w、1m、3m、6m、1y 或 ytd',
    'bad_date': 'start/end 必须是 YYYY-MM-DD',
    'too_long': '自定义区间最长约 3 年',
}


def _positive_int(request, key, default, maximum=None):
    """市场接口口径：非法值抛 ValueError（→400），即 common.params.positive_int(strict=True)。"""
    return positive_int(request.query_params, key, default, maximum, strict=True)


def _optional_float(request, key):
    raw = request.query_params.get(key)
    if raw in (None, ''):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f'{key} 必须是数字')
    if value < 0:
        raise ValueError(f'{key} 不能小于 0')
    return value


def _market_bad_request(loader, label='市场数据'):
    try:
        return Response(_call_with_deadline(loader, label))
    except (UpstreamTimeout, UpstreamCircuitOpen) as e:
        return _deadline_error_response(e, label)
    except ValueError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception:
        return _upstream_failed(label)


@api_view(['GET'])
def market_sectors(request):
    """板块资金轮动：行业或概念的当日完整排行。"""
    def load():
        return get_sector_rotation(
            board=request.query_params.get('board', 'industry'),
            period=request.query_params.get('period', 'day'),
            q=(request.query_params.get('q') or '').strip(),
            sort=request.query_params.get('sort', 'net'),
            order=request.query_params.get('order', 'desc'),
            page=_positive_int(request, 'page', 1),
            page_size=_positive_int(request, 'page_size', 50, maximum=100),
        )
    return _market_bad_request(load, '板块资金轮动')


@api_view(['GET'])
def market_national_etf(request):
    """国家队相关 ETF 观察名单（非官方持仓）。"""
    return _market_response(get_national_team_etfs, '国家队相关ETF观察')


@api_view(['GET'])
def market_national_etf_flow(request):
    """国家队 ETF 区间资金流向：?period=1d|3d|5d|1w|1m|3m|6m|ytd 或 ?start=&end= 自定义。"""
    def load():
        return get_national_team_flow(
            period=request.query_params.get('period', '3m'),
            start=request.query_params.get('start'),
            end=request.query_params.get('end'),
        )
    return _market_bad_request(load, '国家队ETF资金流向')


@api_view(['GET'])
def market_etf_radar(request):
    """ETF 快照雷达：范围、筛选、排序与分页。"""
    def load():
        return get_etf_share_radar(
            scope=request.query_params.get('scope', 'equity_broad'),
            rank=request.query_params.get('rank', 'share'),
            sort=request.query_params.get('sort') or None,
            order=request.query_params.get('order') or None,
            q=(request.query_params.get('q') or '').strip(),
            min_turnover=_optional_float(request, 'min_turnover'),
            page=_positive_int(request, 'page', 1),
            page_size=_positive_int(request, 'page_size', 50, maximum=100),
        )
    return _market_bad_request(load, 'ETF雷达')


@api_view(['GET'])
def market_etf_detail(request, code):
    """单 ETF 当前快照与按需价格历史。"""
    norm = normalize_stock_code(code)
    if norm is None:
        return Response({'detail': 'ETF 代码须为 6 位数字'}, status=status.HTTP_400_BAD_REQUEST)
    return _market_bad_request(lambda: get_etf_detail(
        code=norm,
        range_name=request.query_params.get('range', '3m'),
        start_date=request.query_params.get('start_date') or None,
        end_date=request.query_params.get('end_date') or None,
    ), 'ETF详情')


@api_view(['GET'])
def market_stock_margin(request, code):
    """个股两融余额（交易所官方披露，T+1）。"""
    # 路由是 <str:code>，先校验格式再进入上游请求与缓存键（SEC-08）
    norm = normalize_stock_code(code)
    if norm is None:
        return Response({'detail': '股票代码须为 6 位数字'}, status=status.HTTP_400_BAD_REQUEST)
    return _market_bad_request(lambda: fetch_stock_margin(norm), '个股两融')


@api_view(['GET'])
def market_fund_flow_window(request):
    """大盘主力资金流区间聚合：?period= 档位 或 ?start=&end= 自定义。"""
    def load():
        window = resolve_period(request.query_params.get, FULL_PRESETS, default='1m')
        return get_market_fund_flow_window(window)
    return _market_bad_request(load, '大盘资金流')


@api_view(['GET'])
def market_northbound_window(request):
    """北向资金净买额区间聚合：?period= 档位 或 ?start=&end= 自定义。"""
    def load():
        window = resolve_period(request.query_params.get, FULL_PRESETS, default='1m')
        return get_northbound_window(window)
    return _market_bad_request(load, '北向资金')


@api_view(['GET'])
def market_institutions(request):
    """机构持仓：按股汇总 / 按机构变动 / 北向序列；?code= 可选个股明细，
    ?quarter=2026Q1 可指定机构持股汇总的报告期（默认最近有数据的季度）。"""
    code = (request.query_params.get('code') or '').strip() or None
    quarter = (request.query_params.get('quarter') or '').strip() or None
    try:
        return Response(_call_with_deadline(
            lambda: get_institution_holdings(stock_code=code, quarter=quarter), '机构持仓'))
    except (UpstreamTimeout, UpstreamCircuitOpen) as e:
        return _deadline_error_response(e, '机构持仓')
    except ValueError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception:
        return _upstream_failed('机构持仓')
