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

# 指数/个股：成交量口径的平静判定参数（#2 regime 化）
# 「此前平静」用滞后窗口判（窗口止于 T−calm_lag），近 calm_lag 日的偏离留给
# 维度去测；进入平静用 *_pct，维持平静放宽到 exit_pct（迟滞），避免单日噪声
# 让平静状态忽进忽出（旧实现次日保持率仅约 30%）。
PRICE_CALM_PARAMS = {
    'rvol_window': 10,   # RVOL 基期：前 N 日均量
    'cv_window': 5,      # 量能波动收窄 / 平均真实波幅的「周尺度」窗口
    'rvol_min': 0.7,     # 当日量 / 前10日均量 下限（量能塌陷不算「平静」）
    # 上限只排除单日事件式放量：温和放量正是「量先于价」要找的形态，
    # 旧上限 1.5 会把它挡在门外（#2）
    'rvol_max': 2.5,
    'lookback': 60,      # 自身历史分位的取样窗口
    'vol_cv_pct': 0.4,   # 滞后窗口量能变异系数 ≤ 自身历史 40 分位 → 进入平静
    'amp_pct': 0.4,      # 滞后窗口平均真实波幅(TR) ≤ 自身历史 40 分位 → 进入平静
    'exit_pct': 0.6,     # 已平静时两项都 ≤ 60 分位即维持（迟滞带）
    'calm_lag': 3,       # 「此前平静」窗口止于 T−3，近 3 日留给维度
    'hysteresis_days': 20,  # 迟滞状态机从 T−20 起逐日推进
    'regime_min_samples': 20,  # 分位基线最少样本数，不足的日子不改变状态
    'move_z': 2.5,       # 价格尚未大动：近 calm_lag 日累计涨跌 ≤ move_z·σ·√calm_lag
    'tr_spike_pct': 0.9,  # 当日真实波幅 ≤ 自身历史 90 分位（排除单日剧烈波动）
}
# 板块：快照口径（无成交量）的平静判定参数，regime 口径同上
SECTOR_CALM_PARAMS = {
    'window': 5,
    'flow_pct': 0.5,   # 滞后窗口近3日平均|净额| ≤ 自身历史中位数 → 资金面平稳
    'chg_pct': 0.4,    # 滞后窗口近5日涨跌幅标准差 ≤ 自身历史 40 分位 → 波动低位
    'exit_pct': 0.6,
    'calm_lag': 3,
    'hysteresis_days': 15,
    'regime_min_samples': 8,   # 板块快照只有约 45 行，基线样本门槛相应放低
    'move_z': 2.5,
    'flow_spike_pct': 0.95,    # 近3日平均|净额| 超过历史 95 分位 = 资金突变，不是渐进
}
# 振幅低位判定所需的最少历史振幅样本数（不足则不判定平静）
CALM_MIN_AMP_SAMPLES = 10
# 板块资金面平稳：比较「近 N 日平均|净额|」的窗口长度
SECTOR_FLOW_DAYS = 3

# ============================================================
# 投票与分级（signals.py）
# ============================================================

# 同侧最少「信息组」票数（#4）：维度先按信息源分组，每组至多一票（组内取强），
# 量能强度组只做确认；同组多个维度不再重复计票（见 signals 模块 docstring）
VOTE_MIN_DIMS = 2
# 同侧加权票下限（#4 / 权重参与投票）：组票的权重取组内代表维度的
# DetectorDimConfig.weight（默认 1.0，自动调参边界 [0.5, 2]）。1.5 = 两组默认
# 权重 2.0 留出余量：一组被降到 0.5 时需要另一组 ≥1.0 才能凑够
VOTE_MIN_WEIGHT = 1.5
# 对侧加权票 < 同侧加权票 × 该比例才出信号（旧口径「对侧 0 票」任何噪声票都能否决）
OPPOSE_RATIO = 0.5
# 维度角色：方向组决定 up/down；强度组（量能/活跃度）只做确认与加分，
# 不决定方向、不能否决。注册表条目的 'group' / 'role' 字段优先，
# 未声明的维度各自成组、按方向组处理（与旧口径一致）。
ROLE_DIRECTION = 'direction'
ROLE_STRENGTH = 'strength'

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
# 价格类维度（vol_compress / ma_relation）的噪声尺度由长窗口日收益 σ 解析推导
# （#1/#3/#10）：取近 sigma_window 个日收益，至少 sigma_min 个才可用
SIGMA_DEFAULTS = {'sigma_window': 60, 'sigma_min': 20}
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

# 多重检验（scan.py，#11）
# ============================================================

# 零假设（随机游走、平静日）下各维度的两侧合计投票率，用于估计每次扫描的
# 期望假阳数。个股四维取 regime 化平静闸门下的随机游走复测（σ=1.5%，
# 773 个平静样本，见 DET_NOTES_calm-vote.md）；板块净额沿用 01_code_audit §2.0
# 的板块模拟（19%~21%）；融资余额无模拟，取名义 5%。
# 只用于「报告」，不参与判定；未列出的维度按 1 − noise_pct 估计（偏保守）。
NULL_VOTE_RATES = {
    'big_order_net': 0.20,
    'vol_ratio_slope': 0.15,
    'vol_compress': 0.07,
    'turnover_pct': 0.015,
    'margin_north': 0.05,
    'ma_relation': 0.025,
}
# 按对象类型覆盖：板块（σ≈1.2% 的合成序列）上 ma_relation 平静日投票率约 10%、
# vol_compress 约 9%，与个股差异大（板块随机游走复测，693 个平静样本）
NULL_VOTE_RATES_BY_KIND = {
    'industry': {'ma_relation': 0.10, 'vol_compress': 0.09},
    'concept': {'ma_relation': 0.10, 'vol_compress': 0.09},
}
NULL_VOTE_RATE_FALLBACK = 0.2

# 每日板块信号按强度排序截断的默认上限（settings.DETECTOR_SECTOR_SIGNAL_CAP
# 可覆盖，0 = 不截断）。只截板块：约 500 个板块同日检验、彼此相关，是多重
# 检验假阳的主要来源；指数与自选股是用户明确关注的少量对象，不截。
SECTOR_SIGNAL_CAP_DEFAULT = 10
