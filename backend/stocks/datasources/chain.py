"""个股数据源优先级与多源 failover（从 services 拆出，ARC-01）。

数据源优先级（自动切换）：东方财富 → 新浪 → BaoStock → 腾讯；名称查询优先轻量源。
failover 本身走 market._sources.SourceChain（ARC-03，全项目唯一实现）。
"""

import logging
import time

import pandas as pd

from .baostock import BaoStockSource
from .base import _filter_minute_range
from .eastmoney import EastMoneySource
from .sina import SinaSource
from .tencent import TencentSource

logger = logging.getLogger(__name__)

# 数据源列表（按优先级排序）
DATA_SOURCES = [EastMoneySource(), SinaSource(), BaoStockSource(), TencentSource()]
# 名称查询优先轻量源，避免东财全市场快照拖垮 1H2G
NAME_SOURCES = [BaoStockSource(), SinaSource(), EastMoneySource()]


def _stock_backoff(attempt):
    """个股链路换源前的等待：第 1、2 个源失败后 1s，之后 2s（与原顺序重试一致）"""
    return min(2 ** attempt, 2)


def _source_call(source, method_name):
    """把数据源方法包成 SourceChain 候选；分钟线按请求区间过滤（DAT-01）"""
    def call(*args, **kwargs):
        result = getattr(source, method_name)(*args, **kwargs)
        if method_name == 'fetch_minute' and len(args) >= 3:
            # 按请求区间过滤；过滤后为空（源返回的全是别的日子）视为该源无数据
            before = len(result) if isinstance(result, pd.DataFrame) else 0
            result = _filter_minute_range(result, args[1], args[2])
            if before and (result is None or result.empty):
                logger.info(
                    f"数据源 {source.name} 分钟线 {before} 根均不在 "
                    f"{args[1]}~{args[2]} 内，尝试下一个"
                )
        return result
    return call


def _try_sources(method_name, *args, sources=None, **kwargs):
    """
    依次尝试多个数据源，成功则返回 (source, result)，全部失败返回 (None, None)

    走统一的 SourceChain（ARC-03）：保持原有顺序与换源退避（1s, 1s, 2s），并获得
    源冷却——同一源同一方法连续抛异常 2 次后冷却 180s，冷却期内直接跳过。
    空结果（该区间确实无数据）只换下一个源，不计入冷却。

    Args:
        method_name: 数据源方法名 'fetch_daily' / 'fetch_minute' / 'fetch_stock_name'
        sources: 可选数据源列表，默认 DATA_SOURCES
    """
    from ..market._sources import SourceChain

    source_list = sources if sources is not None else DATA_SOURCES
    by_name = {}
    candidates = []
    for source in source_list:
        chain_name = f'stock_{source.name}_{method_name}'
        by_name[chain_name] = source
        candidates.append((_source_call(source, method_name), chain_name))
    chain = SourceChain(
        backoff=_stock_backoff, empty_is_failure=False,
        sleep=lambda sec: time.sleep(sec),  # 运行时取 time.sleep（测试可打桩）
    )
    result, chain_name = chain.run(candidates, *args, **kwargs)
    if chain_name is None:
        logger.error(f"所有数据源获取 {method_name} 均失败或无数据")
        return None, None
    source = by_name[chain_name]
    logger.info(f"数据源 {source.name} 获取 {method_name} 成功")
    return source, result
