# P0 修复说明：提醒与探测器（分支 fix/p0-alerts-detector）

## 结果
| 条目 | 状态 | 提交 |
|---|---|---|
| ALG-02 | 已修复 | 2f7b213 |
| DAT-08 | 已修复（未改 scheduler.py） | eb4d68d |
| ALG-01 | 已修复 | 18e1223 |
| ALG-03（额外修复） | 已修复 | 5545167 |
| 测试稳定性（额外，ALG-01 附带） | 已修复 | feb29db |

测试：断网全量 `stocks` 共 228 个（基线 214 + 新增 14），只有 1 个失败，即已知的 `tests_features.EtfHistSourceRoutingTests.test_sina_fallback_when_em_fails`。没有发生网络访问。`ruff check .` 全部通过。
注意：运行测试需要 `DJANGO_SECRET_KEY=test DJANGO_DEBUG=True`（settings 在缺少 key 时拒绝启动）。

## ALG-02 提醒回退日线时字段写错，评估循环缺少隔离
- `alerts.py`：`_current_price` 回退日线时，`daily.close` 改为 `daily.close_price`。
- 单条规则的评估抽成 `_evaluate_one(alert, today)`，`evaluate_alerts` 循环里给每条加 `try/except` 和 `logger.exception`。一条规则出错只跳过它自己，不会中断本轮其他规则。
- 测试（`tests_p0_alerts_detector.py::AlertDailyFallbackTests`）：没有分钟 bar 时按日线收盘价触发；一条规则抛异常时，另一条照常触发，并且有 ERROR 日志。

## DAT-08 提醒读到陈旧数据
判断全部放在 `alerts.py` 内部，**没有改 scheduler.py**。`_intraday_job` 在 09:00–09:25 照旧会调用，但 `evaluate_alerts` 会直接返回 `[]`。
- `evaluate_alerts(now=None)`：新增可选参数 `now`，用于测试注入，默认取当前本地时间。有三种情况直接返回 `[]`：
  - 非交易日：`is_trading_day(today)`；
  - 盘前，即 09:30 之前；
  - 午休，即 11:30 之后、13:00 之前。
  收盘后（15:00 以后）照常评估，供 15:10 的收盘汇总任务使用。
- 价格规则：先取当日最新分钟 bar（`datetime__date=today`）；没有就退回当日日线的 `close_price`；两样都没有就不评估。消息里会注明数据来源是“现价”还是“收盘价”。
- 涨跌幅规则：先用当日日线的 `change_pct`。盘中当日日线还没落库时，按「当日最新分钟价 ÷ 上一交易日日线收盘价 − 1」实时计算。**不再读取昨日的 `change_pct`。**
- 去重名额：陈旧数据根本不进入评估，所以不会写入 `AlertEvent`，也就不会占掉当日的去重名额。
- `_hit` 的签名从 `(alert, price, daily)` 改为 `(alert, price, pct)`。全仓没有其他调用方。
- 测试改动：`tests_features.AlertEvaluationTests` 原本把数据写死在 2026-09-04、评估不带时刻。现在数据改成落在“今天”，评估时刻固定为 10:35（三个原有用例的语义不变）。
- 新增测试 `AlertStaleDataTests`：
  - 盘前（09:00、09:25）和午休（12:00）都不评估；
  - 当日没有数据时，昨日 +6% 不会触发；
  - 陈旧数据不占名额：后来当日数据到达后仍能正常触发；
  - 盘中涨跌幅按当日价 ÷ 昨收计算，不用昨日的 `change_pct`；
  - 收盘后用当日日线；
  - 非交易日跳过。
- 已知局限，需要在 VPS 上观察：
  1. 盘中实时涨跌幅的昨收，取的是“库里今天以前最新的一条日线”。如果昨天的收盘汇总失败、缺了昨日日线，就会拿前天的收盘价当昨收，算出的涨跌幅会偏。这种情况依赖 data-ingest 方向保证日线完整，我没有额外拦截。
  2. 判断依赖 Django 当前时区（`timezone.localtime` 与 `datetime__date`），生产环境必须是 Asia/Shanghai。
  3. 如果 data-ingest 方向给 MinuteBar 加了周期字段（例如 5 分钟的 BaoStock bar），当日最新 bar 也可能来自 5 分钟 bar。这对“最新价”没有影响。

## ALG-01 平静判定遇到 None 崩溃，扫描缺少单对象隔离
- `detector/calm.py`：
  - 当前 5 日量能 CV：要求 5 根量齐全、均值非零、`_std` 不为 None，否则视为“量能样本不足”，返回 `(False, reason)`。对应原 calm.py:71。
  - 板块：当前涨跌幅窗口里有 None 时，返回 `'涨跌幅窗口存在缺失快照'`（与净额窗口的口径一致）。`_percentile(roll_std)` 为 None 时，返回 `'涨跌幅滚动样本不足'`。对应原 calm.py:123-131。
- `detector/scan.py`：`calm_mod.is_calm`、`signals.evaluate_target`、`signals.upsert_signal` 三处各自加 `try/except`：
  - 用 `logger.exception` 记日志；
  - 往 `degraded` 追加一条 `{'target': 'kind:code', 'reason': '平静判定异常: ...'}`；
  - 跳过这个对象，继续扫描。
  失败数通过已有字段 `DetectorRun.degraded`（列表）和 `status='partial'` 体现。没有新增字段，没有改 models。
- 测试：`CalmNoneHandlingTests` 3 个、`ScanTargetIsolationTests` 1 个。已在旧代码上验证，这 4 个用例都会复现 TypeError 或断言失败。

## ALG-03（额外修复）`detector/views.py` 先切片后 filter
- `all_dates=1` 时，先加近 30 天的日期窗（`trade_date__gte=今天-30`），等所有筛选参数都 filter 完，再执行 `order_by('-trade_date')[:300]`。行为与 docstring 写的“近 30 天”一致，上限仍是 300 行。
- 测试 `DetectorSignalsAllDatesTests`：`all_dates=1&severity=high` 返回 200，且 45 天前的信号被排除。

## 额外修复：测试的墙钟依赖
`tests_detector` 里 `SectorSignalScanTests` 和 `DegradedScanTests` 两个用例调用 `run_scan()` 时没有固定时钟。在 15:30 前运行会命中守卫，返回 `{'skipped': 'before_close'}`，从而报 `KeyError: 'status'`。
本次在北京时间 00:11 运行时，基线就出现了这 2 个 ERROR；基线的 214/1 应该是在 15:30 之后跑出来的。
修法：只 patch `scan` 模块里的 `timezone`，把时钟固定在今天 16:00。没有改任何业务逻辑。

## 涉及文件
- `backend/stocks/alerts.py`
- `backend/stocks/detector/calm.py`
- `backend/stocks/detector/scan.py`
- `backend/stocks/detector/views.py`
- `backend/stocks/tests_p0_alerts_detector.py`（新建）
- `backend/stocks/tests_features.py`：只改了 `AlertEvaluationTests` 那一段，以及 datetime 的 import 行。合并时可能与其他代理对该文件的改动冲突，属于局部冲突。
- `backend/stocks/tests_detector.py`：时钟 patch。

没有改 scheduler.py、models.py、services.py、market/、ai/、settings.py、前端。

## 运维须知
- 不需要迁移，也不需要回刷数据。
- 部署后，可以在 VPS 上观察以下几点：
  - 09:00–09:25 不再出现提醒推送；
  - 盘中涨跌幅提醒在当天真正达到阈值时才触发；
  - 日志里是否出现 `价格提醒 #… 评估失败` 和 `探测器平静判定异常`。如果出现，按日志里的对象排查数据。
- 历史上已经误报的 `AlertEvent`（盘前用昨日数据写入、trade_date 为当天的那些）不会被自动清理。如需清理，请人工处理。
