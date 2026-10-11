"""兼容别名：内置选股策略已迁到 stocks.screener.strategies（ARC-07）。"""
import sys

from ..screener import strategies as _strategies

sys.modules[__name__] = _strategies
