"""条件选股（与 LLM 无关的纯规则引擎）：engine 为 DSL 与执行，strategies 为内置模板。

AI 选股（stocks.ai.views.screener_ai）只负责把自然语言翻译成 DSL，再复用这里的引擎。
"""
