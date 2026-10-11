"""探测器离线回测与评估工具（00_plan 阶段 1，对应 manage.py detector_backtest）。

为什么要离线回测：生产的在线复盘（review.py）样本极少——40 只股票 3 年只有
个位数 active 段首信号，自动调参永远凑不够样本；参数是否有效只能靠历史
逐日复算来判断。

原则：
- 不复刻算法：平静闸门 calm.is_calm、各维度 evaluate、投票 signals.evaluate_target、
  复盘判定 review._evaluate_bars 全部直接调用仓库函数；只通过正式注入点替换
  「查库的周期计数」（MemoryCycleCounter）与「联网的交易日历」（ListCalendar）；
- 严禁前视：T 日的 ctx 只由 T 及之前的数据构造（context.build_ctx），前瞻收益
  与复盘判定单独计算、只用于评估；
- 不写库：结果只输出 JSON/Markdown 报告，绝不写 DetectorSignal/DetectorDimConfig，
  不污染生产调参；
- 数据走本地文件缓存（data.DataStore），缓存格式与 detector-research/backtest/data
  原型一致，可直接复用；--offline 时只读缓存、不联网。

模块：
- tradedays  本地交易日历（三态 is_trading_day，可注入 signals/review/scan）
- data       数据加载 + 文件缓存（baostock 个股前复权日线、新浪指数日线与流通股本、两融宏观表）
- context    按时点构造 ctx（防前视）
- engine     逐标的逐日回放：平静 → 维度 → 投票/周期 → 复盘判定与前瞻收益
- metrics    评估指标：触发数、平静通过率、命中率 vs 随机基线（重采样 p/z）、超额分布与 IC、分组
- synthetic  植入形态的合成检测力测试
- report     Markdown 报告
"""
