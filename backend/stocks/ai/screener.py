"""兼容别名：选股引擎已迁到 stocks.screener.engine（ARC-07）。

整模块别名而不是逐个 re-export：旧路径上的 import、mock.patch('stocks.ai.screener.X')
与新路径指向同一个模块对象，行为完全一致。新代码请直接用 stocks.screener.engine。
"""
import sys

from ..screener import engine as _engine

sys.modules[__name__] = _engine
