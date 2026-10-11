"""探测器阈值与口径常量：平静闸门、投票分级、回看调参、序列构建的唯一来源。

为什么集中到这里（ARC-06 / A-28 / ALG-15）：这些数值原先散落在 calm、
signals、review、dimensions 四个模块，配置页（detector/views.detector_config）
再各自去取，改一处容易漏另一处。现在实现与展示都引用这里的同一份常量。

约定：
- 只放「固定口径」的常量；会被 review 自动微调的维度参数（min / 权重）仍以
  registry.DIMENSIONS 的 defaults 为默认值、DetectorDimConfig 为当前生效值；
- 各模块保留同名引用（如 review.MIN_ADJ_SAMPLES），旧的 import 路径照常可用；
- 数值调整须同步看 tests_p2_detector 的随机游走回归测试（噪声信号率 < 5%）。
"""

from __future__ import annotations

# ============================================================
# 平静闸门（calm.py）
# 平静参数不参与自动迭代，理由见 calm.py 文件尾。
# ============================================================

# 指数/个股：成交量口径的平静判定参数
PRICE_CALM_PARAMS = {
    'rvol_window': 10,   # RVOL 基期：前 N 日均量
    'cv_window': 5,      # 量能波动收窄的「周尺度」窗口
    'rvol_min': 0.7,     # 当日量 / 前10日均量 下限
    'rvol_max': 1.5,     # 上限：超过 1.5 已经「不平静」
    'lookback': 60,      # 自身历史分位的取样窗口
    'vol_cv_pct': 0.4,   # 近5日量能变异系数 ≤ 自身历史 40 分位 → 收窄
    'amp_pct': 0.4,      # 当日振幅 ≤ 自身历史 40 分位 → 振幅低位
}
# 板块：快照口径（无成交量）的平静判定参数
SECTOR_CALM_PARAMS = {
    'window': 5,
    'flow_pct': 0.5,   # 近3日平均|净额| ≤ 自身历史中位数 → 资金面平稳
    'chg_pct': 0.4,    # 近5日涨跌幅标准差 ≤ 自身历史 40 分位 → 波动低位
}
# 振幅低位判定所需的最少历史振幅样本数（不足则不判定平静）
CALM_MIN_AMP_SAMPLES = 10
# 板块资金面平稳：比较「近 N 日平均|净额|」的窗口长度
SECTOR_FLOW_DAYS = 3

# ============================================================
# 投票与分级（signals.py）
# ============================================================

# 同向投票的最少维度数与「零反向票」约束（见 signals 模块 docstring 的取舍说明）
VOTE_MIN_DIMS = 2

# 严重程度分级阈值：score 是同侧维度 |value| 的加权平均（0-100），
# 55/40 两条线让「多维度共振+多周期维持」显著高于「勉强两票」。
SEVERITY_RULES = {'high_score': 55.0, 'medium_score': 40.0}
# 高等级还要求同侧至少这么多维度共振
HIGH_SEVERITY_MIN_VOTES = 3
# 周期数达到它即「维持中」（active）；周期 1 为 watching（观察中）
ACTIVE_MIN_CYCLES = 2

# 周期计数最多回看的交易日数（与旧实现的 20 次循环同一上限）
CYCLE_LOOKBACK = 20

# ============================================================
# 回看与自动调参（review.py，口径见 review 模块 docstring）
# ============================================================

REVIEW_LOOKBACK = 5
HIT_SIGMA_K = 1.0       # 兑现阈值 = k × σ20
SIGMA_WINDOW = 20       # σ 取 T 日及之前 20 个日涨跌幅
HIT_PCT = 1.5           # σ 不可得时的兜底阈值（%）
BASELINE_DAYS = 60      # 无条件基线的起点数上限（T 日之前）
MIN_BASELINE_WINDOWS = 20
# 待回看信号只追溯这么多自然日：数据缺口长期补不齐的信号不再每天重试
REVIEW_MAX_AGE_DAYS = 60
# 样本不足不动参数；同标的连续信号只计段首，样本量门槛相应提高
MIN_ADJ_SAMPLES = 10
# 有界调整：步长 5%，权重/阈值的绝对边界
STEP = 1.05
WEIGHT_BOUNDS = (0.5, 2.0)
MIN_SCALE_BOUNDS = (0.5, 2.0)
# 超额命中率（命中率 − 基线）：±WEIGHT_BAND 之外才调权重；低于 EXCESS_LOW →
# 投票太宽松（提高阈值），高于 EXCESS_HIGH → 太苛刻（放宽）
WEIGHT_BAND = 0.05
EXCESS_LOW, EXCESS_HIGH = -0.15, 0.15
# 段首 active 信号的周期数（周期 1 是 watching，2 是首个 active 日）
SEGMENT_HEAD_CYCLES = 2

# ============================================================
# 序列构建与维度兜底（dimensions.py / registry.py）
# ============================================================

# index/stock 目标的最低历史门槛：低于它连平静判定的滚动窗口都凑不齐，
# 整个对象跳过（如实在 run.degraded 里记录原因）。
MIN_PRICE_BARS = 30
# sector 目标（快照派生序列）的最低有效天数：覆盖 ma20/波动窗口的底线。
MIN_SECTOR_POINTS = 12
# 融资余额只在这些指数上投票（ALG-09）：两融是沪深合计的全市场口径，对 5 个
# 指数完全相同，逐个投票等于同一事实计 5 票、信号高度相关并在回看里被当成独立
# 样本；沪深300 横跨沪深两市，作为全市场的代表。
MARGIN_VOTE_CODES = ('sh000300',)
# 板块快照的回看自然日数
SECTOR_LOOKBACK_DAYS = 45

# 噪声闸门默认值（各价量维度共用，见 registry 模块 docstring）；
# dimensions 里参数缺省时的兜底也取这里，不再另写一份。
# - noise_window 120：上下文拉长到约 250 根后取满 120 个历史 raw，70 根时有多少
#   用多少（#10）；分位用 Harrell-Davis 估计，小样本下比线性插值稳；
# - noise_hi 2.5：上限 = 2.5 × 自身噪声带，取代跨品种的绝对 max（#1）。
NOISE_DEFAULTS = {'noise_pct': 0.8, 'noise_window': 120, 'noise_min': 15, 'noise_hi': 2.5}
# 波动率压缩：当前宽度 ≤ 自身历史该分位才算压缩（registry 默认值与兜底共用）
COMPRESS_PCT_DEFAULT = 0.25
# 换手率分位：量价基准断层（送转/配股）的台阶比例（registry 默认值与兜底共用）
SPLIT_RATIO_DEFAULT = 1.2

# ============================================================
# 取数上下文长度与日期轴（data-align：#8 #9 #10 #15）
# 只放取数层口径；维度阈值仍在上面各节与 registry。
# ============================================================

# 指数/个股日线上下文根数（#10）：noise_window=60 的噪声闸门、60 日换手率
# 分位与平静基线在 70 根下只能取到 22~56 个重叠样本，拉长到约一年让它们
# 真正取满。个股只读本地 DailyQuote（不足就用现有的，不额外联网）；指数与
# 原来同一次新浪日线请求（上游返回全史，只是 tail 取得更长）。
CONTEXT_BARS = 250
# 板块快照上下文：按交易日历生成的日期轴长度（交易日数，#8/#10）。快照只能
# 本站逐日积累，实际可用长度 = min(本值, 已积累天数)，缺失日为 None。
SECTOR_CONTEXT_DAYS = 120
# 融资余额上下文：日期轴长度（交易日数）；末点固定为指数数据日的前一交易日
# （两融 T+1 披露，扫描日 T 最新只有 T−1 的数，#9）。
MARGIN_CONTEXT_DAYS = 120
# 融资余额维度使用的快照字段（#9）：rz=融资余额（沪+深），不再用融资融券余额 total
MARGIN_VALUE_KEY = 'rz'
# 日期轴往回走的自然日上限 = 交易日数 × 倍数 + 缓冲（覆盖春节/国庆长假）
AXIS_CALENDAR_MULTIPLIER = 2
AXIS_CALENDAR_BUFFER = 30

# 板块资金净额去价格化（#6）：同花顺板块净额按指数涨跌划分流入/流出，与当日
# change_pct 机械同向。逐板块用「截至当日」的滚动窗口做 net ~ a + b·change_pct
# 的最小二乘，取当日残差作为维度输入（只用 ≤ 当日的数据，无前视）。
FLOW_RESID_WINDOW = 60
# 拟合所需最少有效点数：不足时该点残差为 None（如实不可用，不退回原始净额）
FLOW_RESID_MIN_OBS = 20

# 量能序列送转/新股保护（#15，供 calm/量比类维度接入）：
# 新股上市初期的极端量能不进入基期——ctx 首根就是该股在库里的第一根、且
# 不足 CONTEXT_BARS 时，把最前面这么多根量能置 None。
NEW_LISTING_MASK_BARS = 20
