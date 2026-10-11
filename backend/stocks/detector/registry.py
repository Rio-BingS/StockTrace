"""探测器维度注册表：每个微变维度的定义、适用对象、默认参数与启停状态。

为什么用注册表而不是把维度散落在扫描代码里：
1. 「迭代优化」要求权重与阈值是数据（可持久化、可微调、可展示），
   注册表是参数的默认值来源，DetectorDimConfig 表存「当前生效值」；
2. 无数据源的维度在这里显式 enabled=False 并写明原因——让「本站暂不支持
   什么」成为代码与 API 里可见的事实，而不是隐式的沉默缺失（数据诚信红线）；
3. 新增维度 = 注册表加一个条目 + dimensions.py 加一个评估函数，扫描、
   热力图、配置页自动跟上。

params 约定：
- min   投票下限：|原始漂移| 低于它视为「没有微变」；
- mild  温和带上限的归一化基准（value = raw/mild×50）；
- max   突变上限：超过它说明已不是「早期迹象」，如实不投票；
- 迭代优化（review.py）只微调 min（灵敏度）与权重，min 的调整边界是
  [0.5×, 2×] 默认值，权重边界 [0.5, 2.0]，防止自动调参失控；
- noise_pct / noise_window / noise_min  噪声闸门（ALG-04）：|原始漂移| 还须
  超过自身近 noise_window 个历史漂移绝对值的 noise_pct 分位才投票，历史不足
  noise_min 个不投票。min 是跨品种绝对常数，单靠它在随机游走上也会大量出
  信号；噪声闸门不参与自动调参，noise_pct 置 0 即关闭；
- compress_pct / drift_z  波动率压缩专用：当前宽度 ≤ 自身历史 compress_pct
  分位才算压缩，压缩期漂移的 t 值 ≥ drift_z 才投票（2.13≈自由度 4 的双侧 10%）。

校准依据：backend/stocks/tests_p2_detector.py 的随机游走回归测试——默认参数
下平静对象的噪声信号率须 < 5%（修复前约 31%）。
"""

from __future__ import annotations

from . import dimensions as _dims
# 噪声闸门默认值（各价量维度共用，见模块 docstring），数值在 constants.py
from .constants import (
    COMPRESS_PCT_DEFAULT,
    NOISE_DEFAULTS as _NOISE,
    SPLIT_RATIO_DEFAULT,
)

DIMENSIONS = [
    {
        'key': 'big_order_net',
        'name': '板块资金净额',
        # 指数不在 targets（ALG-09）：build_index_context 从未提供净额序列，指数上
        # 该维度恒为「不可用」；大盘资金快照是全市场口径，即便接入也会给 5 个
        # 指数投完全相同的一票（与融资余额同样的相关性放大问题）
        'targets': ('industry', 'concept'),
        'enabled': True,
        'note': (
            '同花顺板块资金净额（全单主动买卖口径，按板块指数涨跌划分流入/流出，非大单/主力），'
            '来自收盘后日度快照积累；先对当日涨跌幅做滚动回归、取残差再看 6 日均值漂移，'
            '剔除与涨跌机械同向的部分。指数暂不参与：大盘资金快照是全市场口径，'
            '对各指数是同一条序列，接入只会重复计票。'
        ),
        'defaults': {'window': 15, 'mean_days': 6, 'mild': 0.5, 'min': 0.08, 'max': 2.0, **_NOISE},
        'evaluate': _dims.eval_big_order_net,
    },
    {
        'key': 'vol_ratio_slope',
        'name': '量比斜率',
        'targets': ('index', 'stock'),
        'enabled': True,
        'note': 'RVOL=当日量/前10日均量，取近4点最小二乘斜率；平静期量比温和抬升是资金回流的最早形态。',
        'defaults': {'window': 10, 'slope_days': 3, 'mild': 0.15, 'min': 0.02, 'max': 0.5, **_NOISE},
        'evaluate': _dims.eval_vol_ratio_slope,
    },
    {
        'key': 'vol_compress',
        'name': '波动率压缩',
        'targets': ('index', 'stock', 'industry', 'concept'),
        'enabled': True,
        'note': (
            '近5日涨跌幅标准差 ≤ 自身历史 25 分位才算压缩，方向取压缩期内价格漂移且须'
            '相对窗口波动显著（t≥2.13）；未压缩不投票。'
        ),
        'defaults': {'window': 5, 'mild': 2.0, 'min': 0.2, 'max': 6.0,
                     'compress_pct': COMPRESS_PCT_DEFAULT, 'drift_z': 2.13,
                     'noise_window': _NOISE['noise_window'], 'noise_min': _NOISE['noise_min']},
        'evaluate': _dims.eval_vol_compress,
    },
    {
        'key': 'turnover_pct',
        'name': '换手率分位',
        'targets': ('index', 'stock'),
        'enabled': True,
        'note': (
            '个股用真实换手率（成交量/新浪流通股本）的60日分位，送转、定增、解禁前后口径连续；'
            '股本数据缺失时退回成交量分位并标注「股本数据缺失」（检测到送转/配股的量价基准断层时暂停）；'
            '指数无股本概念，用成交量分位作活跃度代理。分位从低位缓升（不热）/高位缓降（不死）才投票。'
        ),
        'defaults': {'window': 60, 'delta_days': 5, 'mild': 30.0, 'min': 8.0, 'max': 60.0,
                     'pos_cap': 70.0, 'pos_floor': 30.0, 'split_ratio': SPLIT_RATIO_DEFAULT, **_NOISE},
        'evaluate': _dims.eval_turnover_pct,
    },
    {
        'key': 'margin_north',
        'name': '融资余额',
        'targets': ('index',),
        'enabled': True,
        'note': (
            '融资余额（沪深合计，不含融券），只在沪深300上投票，其余指数不重复计票；'
            '数据为交易所官方 T+1 披露、经本站日度快照积累（可回补），按交易日对齐，'
            '末点为指数数据日的前一交易日；噪声闸门需约 21 个交易日的连续数据才投票。'
        ),
        'defaults': {'mild': 0.5, 'min': 0.06, 'max': 2.0, **_NOISE},
        'evaluate': _dims.eval_margin_north,
    },
    {
        'key': 'ma_relation',
        'name': '价格与均线关系',
        'targets': ('index', 'stock', 'industry', 'concept'),
        'enabled': True,
        'note': (
            '20日均线距离的3日漂移；板块价格由真实日涨跌幅链式合成（基期100）。'
            '距均线过远（趋势已展开）如实不投票。'
        ),
        'defaults': {'ma': 20, 'slope_days': 3, 'mild': 0.6, 'min': 0.08, 'max': 3.0,
                     'max_dist': 4.0, **_NOISE},
        'evaluate': _dims.eval_ma_relation,
    },
    {
        # 数据诚信说明：以下两个维度是任务要求中列出、但本站现有数据源
        # 无法真实计算的，v1 显式停用并在 /detector/config 与前端展示原因。
        # 绝不用代理值或合成数值冒充（CLAUDE.md 红线 #1）。
        'key': 'order_imbalance',
        'name': '委比与挂单结构',
        'targets': (),
        'enabled': False,
        'disabled_reason': '本站无盘口挂单数据源（委比/档位挂单需 Level-1 逐笔委托数据），待接入后启用',
        'note': '需要实时盘口委托队列数据；现有 services/market 无对应接口，不引入不可靠的替代口径。',
        'defaults': {},
        'evaluate': None,
    },
    {
        'key': 'sector_dispersion',
        'name': '板块内分化',
        'targets': (),
        'enabled': False,
        'disabled_reason': '需板块成分股横截面数据；现有日度快照仅含板块净额与涨跌幅，待快照扩字段积累后启用',
        'note': (
            '「板块内个股分化」要求逐板块的成分股涨跌分布；'
            '日度快照当前 payload 只有 {name, net, change_pct}，需先扩字段再积累窗口。'
        ),
        'defaults': {},
        'evaluate': None,
    },
]

# 供热力图/前端固定列顺序使用的启用维度 key 列表
ENABLED_KEYS = [d['key'] for d in DIMENSIONS if d['enabled']]
DIMENSION_MAP = {d['key']: d for d in DIMENSIONS}


def specs_for_target(kind):
    """某对象类型适用的启用维度定义列表。"""
    return [
        d for d in DIMENSIONS
        if d['enabled'] and kind in d['targets']
    ]
