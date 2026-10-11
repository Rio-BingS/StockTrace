"""内置选股策略模板：问财/东财常用的经典条件组合，一键载入可再改。

（ARC-07：原 stocks/ai/strategies.py，stocks.ai.strategies 保留为兼容别名。）

与用户自存预设（ScreenerPreset）区分：模板只读、随代码更新。
spec 里的字段与操作符全部经 run_screener 校验，保证可执行。
"""

# 每项：name + spec（结构与手动条件完全一致）
BUILTIN_STRATEGIES = [
    {
        'name': '均线多头排列',
        'desc': '5>10>20 日线且收盘站上20日线（趋势向上）',
        'spec': {'logic': 'all', 'conditions': [
            {'field': 'ma5_gt_ma10', 'op': 'eq', 'value': 1},
            {'field': 'ma10_gt_ma20', 'op': 'eq', 'value': 1},
            {'field': 'above_ma20', 'op': 'eq', 'value': 1},
        ], 'order_by': 'change_pct', 'order_dir': 'desc'},
    },
    {
        'name': 'MACD 金叉',
        'desc': '今日 DIF 上穿 DEA（最近一根刚发生）',
        'spec': {'logic': 'all', 'conditions': [
            {'field': 'macd_golden_cross', 'op': 'eq', 'value': 1},
        ], 'order_by': 'macd_hist', 'order_dir': 'desc'},
    },
    {
        'name': 'MACD 多头零上',
        'desc': 'DIF 在 DEA 上方且 DIF>0（零轴上方多头）',
        'spec': {'logic': 'all', 'conditions': [
            {'field': 'macd_dif_gt_dea', 'op': 'eq', 'value': 1},
            {'field': 'macd_dif_gt_zero', 'op': 'eq', 'value': 1},
        ]},
    },
    {
        'name': 'KDJ 金叉',
        'desc': '今日 K 上穿 D',
        'spec': {'logic': 'all', 'conditions': [
            {'field': 'kdj_golden_cross', 'op': 'eq', 'value': 1},
        ]},
    },
    {
        'name': 'RSI 超卖',
        'desc': '14 日 RSI ≤ 30（短线超卖，关注反弹）',
        'spec': {'logic': 'all', 'conditions': [
            {'field': 'rsi_14', 'op': 'lte', 'value': 30},
        ], 'order_by': 'rsi_14', 'order_dir': 'asc'},
    },
    {
        'name': '放量创20日新高',
        'desc': '量比 ≥ 2 且创 20 日收盘新高（突破确认）',
        'spec': {'logic': 'all', 'conditions': [
            {'field': 'volume_ratio', 'op': 'gte', 'value': 2},
            {'field': 'new_high_20d', 'op': 'eq', 'value': 1},
        ], 'order_by': 'volume_ratio', 'order_dir': 'desc'},
    },
    {
        'name': '缩量回调不破20日线',
        # 回撤下限 2%（ALG-16）：旧条件 between [-10, 0] 包含 0，正在创新高的股票也算「回调」
        'desc': '距20日最高收盘回撤 2%~10% 且仍站上20日线、量比 ≤ 0.8',
        'spec': {'logic': 'all', 'conditions': [
            {'field': 'pct_from_high_20d', 'op': 'between', 'value': [-10, -2]},
            {'field': 'above_ma20', 'op': 'eq', 'value': 1},
            {'field': 'volume_ratio', 'op': 'lte', 'value': 0.8},
        ]},
    },
    {
        'name': '触及布林下轨（超卖）',
        'desc': '收盘价跌破 BOLL(20,2) 下轨',
        'spec': {'logic': 'all', 'conditions': [
            {'field': 'below_boll_lower', 'op': 'eq', 'value': 1},
        ]},
    },
    {
        'name': '低估值蓝筹',
        # 与描述一致（ALG-16）：between [0, 15] 包含 PE=0（无意义/异常值），改为 gt 0 + lte 15；
        # 「蓝筹」补上总市值门槛（500 亿元，total_mv 单位为元）
        'desc': '0 < 市盈率(TTM) ≤ 15、市净率 ≤ 1.5 且总市值 ≥ 500 亿',
        'spec': {'logic': 'all', 'conditions': [
            {'field': 'pe_ttm', 'op': 'gt', 'value': 0},
            {'field': 'pe_ttm', 'op': 'lte', 'value': 15},
            {'field': 'pb', 'op': 'lte', 'value': 1.5},
            {'field': 'total_mv', 'op': 'gte', 'value': 50000000000},
        ], 'order_by': 'pe_ttm', 'order_dir': 'asc'},
    },
    {
        'name': '连续上涨',
        'desc': '收盘连涨 ≥ 3 天',
        'spec': {'logic': 'all', 'conditions': [
            {'field': 'up_days', 'op': 'gte', 'value': 3},
        ]},
    },
    {
        'name': '大涨放量',
        'desc': '当日涨 ≥ 6% 且量比 ≥ 1.5（资金关注度骤升）',
        'spec': {'logic': 'all', 'conditions': [
            {'field': 'change_pct', 'op': 'gte', 'value': 6},
            {'field': 'volume_ratio', 'op': 'gte', 'value': 1.5},
        ], 'order_by': 'change_pct', 'order_dir': 'desc'},
    },
]
