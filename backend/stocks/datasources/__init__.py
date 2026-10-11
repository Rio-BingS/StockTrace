"""个股行情数据源适配层（ARC-01：从 services 拆出）。

- base：DataSource 基类、列名归一与单位换算工具；
- eastmoney / sina / tencent / baostock：各上游适配器；
- chain：数据源优先级（DATA_SOURCES / NAME_SOURCES）与多源 failover（_try_sources）。

注意打桩：_try_sources 运行时读取 chain.DATA_SOURCES，测试替换数据源列表请 patch
'stocks.datasources.chain.DATA_SOURCES'。
"""

from .baostock import BaoStockSource
from .base import (
    DataSource,
    _filter_minute_range,
    _normalize_ohlcv,
    _to_numeric_cols,
    _volume_lots_to_shares,
)
from .chain import DATA_SOURCES, NAME_SOURCES, _source_call, _stock_backoff, _try_sources
from .eastmoney import EastMoneySource
from .sina import SinaSource
from .tencent import TencentSource

__all__ = [
    'DataSource',
    'EastMoneySource',
    'SinaSource',
    'TencentSource',
    'BaoStockSource',
    'DATA_SOURCES',
    'NAME_SOURCES',
    '_try_sources',
    '_source_call',
    '_stock_backoff',
    '_filter_minute_range',
    '_normalize_ohlcv',
    '_to_numeric_cols',
    '_volume_lots_to_shares',
]
