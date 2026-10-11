"""兼容别名：AI 序列化器已迁到 stocks.ai.serializers（ARC-07）。

整模块别名而不是逐个 re-export：旧路径上的 import、mock.patch('stocks.ai_serializers.X')
与新路径指向同一个模块对象，行为完全一致。新代码请直接用 stocks.ai.serializers。
"""
import sys

from .ai import serializers as _serializers

sys.modules[__name__] = _serializers
