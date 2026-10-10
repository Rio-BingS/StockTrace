# P0 修复说明：个股数据入库（分支 fix/p0-data-ingest）

负责条目：DAT-01、PERF-01、DAT-03、DAT-02、DAT-07，外加修复 tests_features 写死日期的常红用例。

## 测试结果
- `nonet_run.py stocks`：**Ran 239，0 failures，2 errors**（基线 214 个：1 failure + 2 errors）。
  - 已修：`tests_features.EtfHistSourceRoutingTests.test_sina_fallback_when_em_fails`（日期改为相对今天）。
  - 剩下 2 个 error **基线就有，不是本分支引入**：`tests_detector.DegradedScanTests.test_all_sources_down_produces_no_fabricated_signals`、`SectorSignalScanTests.test_calm_sector_with_two_aligned_dims_fires_signal`。原因是依赖当天时刻：`run_scan` 在本地时间 15:30 前返回 `{'skipped': 'before_close'}`，没有 `status` 键。在 00:xx（CST）跑会红，15:30 后跑会绿。归 detector 方向处理（测试里 patch `timezone.localtime` 或传过去的 trade_date）。COMMON.md 写的"基线 1 个失败"没有算上这两个。
- 新增 25 个测试，见 `backend/stocks/tests_p0_data_ingest.py`。
- `ruff check .`：通过。`makemigrations --check`：No changes。
- 测试需要的环境变量：`DJANGO_SECRET_KEY=test DJANGO_DEBUG=True`（settings 缺 key 时拒绝启动）。

## 提交
1. `042452e` test(P0)：ETF 新浪兜底用例日期改为相对今天
2. `7346310` fix(P0 DAT-01)
3. `fedd8c5` fix(P0 PERF-01)
4. `df928e0` fix(P0 DAT-03)
5. `7f8806f` fix(P0 DAT-02)
6. `2ff4aec` fix(P0 DAT-07)

## 涉及文件
- `backend/stocks/services.py`（主要改动）
- `backend/stocks/models.py`（DailyQuote.source；MinuteBar.source/freq；volume/turnover 的 verbose_name 标注单位）
- `backend/stocks/migrations/0009_dailyquote_source_minutebar_source_freq.py`（新增）
- `backend/stocks/management/commands/rebuild_high_low_times.py`（新增）
- `backend/stocks/management/commands/fetch_stock_data.py`（新增 `--full-daily`、`--source`）
- `backend/StockTrace/settings.py`（**只改了 DATABASES 段**：后面追加了按版本开启 `transaction_mode`）
- `backend/stocks/tests_features.py`（只改了 ETF 新浪兜底用例的日期）
- `backend/stocks/tests_p0_data_ingest.py`（新增）

## DAT-01 新浪分钟线忽略日期（已修复）
- 新增 `_filter_minute_range`。`_try_sources('fetch_minute', symbol, start_dt, end_dt)` 在 failover 层按请求的 [start, end] 过滤。过滤后为空就视为该源失败，换下一个源。
- `fetch_minute_data` 求极值前再按交易日过滤一次（纵深防御）。极值改用公共函数 `compute_high_low_times`，并列时取最早一根。回写由 `get()+save()` 改为 `filter().update(high_time, low_time)`（DB-24）。
- 默认 trade_date 改用 `timezone.localdate()`，原来用 UTC 日期，凌晨会落到前一天。`fetch_stock_all_data` 的 today 同样改了。
- 新命令 `rebuild_high_low_times [--dry-run] [--code X] [--since YYYY-MM-DD]`：用已有 MinuteBar 按本地交易日重算。同一天如果混有不同周期，只用最细周期。
- 测试：只取当日 bar；过滤后为空时切到下一个源；所有源都在区间外时不写库；命令的 dry-run、--code、--since 和全量模式。

## PERF-01 分钟线逐行写（已修复）
- 新增 `_save_minute_bars`：每股每日一个 `transaction.atomic`，先一次读出当日已有 bar，只对新增或数值有变化的 bar 做 `bulk_create(update_conflicts=True, unique_fields=['stock','datetime'], update_fields=[...], batch_size=500)`。盘中每轮通常只写最后一两根。datetime 改为 aware，消除 naive 警告。
- 日线同样改为每股一个事务加 `bulk_create(update_conflicts=True, unique_fields=['stock','trade_date'])`。update_fields 不含 high_time/low_time，分钟线算出的高低点不会被覆盖。同一日期重复时以最后一条为准。
- 已确认版本支持：venv 是 Django 6.1.2，`update_conflicts` 需要 Django ≥ 4.1 和 SQLite ≥ 3.24。唯一约束 `unique_together` 正好是 (stock, datetime) 和 (stock, trade_date)。
- settings：`DATABASES['default']['OPTIONS']['transaction_mode'] = 'IMMEDIATE'`，只在 Django ≥ 5.1 时设置。低版本把这个参数传给 sqlite3.connect 会报 TypeError，所以按版本判断，没有写死。**部署须知**：VPS 的 Django 必须 ≥ 5.1 才会生效。requirements 的下限是 4.2，建议 OPS-02 锁版本时一并确认。
- 测试：只写增量；无变化时不调用 bulk_create；日线 upsert 保留 high_time。

## DAT-03 成交量单位（已修复）
- 适配层统一成"股/元"：
  - 东财日线、分钟线：成交量 ×100（手→股）。
  - 新浪日线、分钟线：原本就是股/元。分钟线原来漏映射了 `amount`，现在映射为成交额，并做了数值化。
  - BaoStock：原本就是股/元，`minute_freq=5`。
  - 腾讯：`amount` 列实际是成交量（手），映射为成交量并 ×100，成交额置 None。这是按 akshare 1.18.64 源码 `stock_feature/stock_hist_tx.py` 核对的，列为 `date,open,close,high,low,amount`。
- 迁移 0009：`DailyQuote.source`、`MinuteBar.source`（CharField(16)，default ''）、`MinuteBar.freq`（PositiveSmallIntegerField，default 1）。另有 4 个 AlterField 只改 verbose_name，不产生 SQL。
- 1 分钟和 5 分钟混存的处理：唯一键保持 (stock, datetime) 不变，**同一交易日只保留一种周期**。写 1 分钟 bar 时删掉当日的粗周期 bar；当日已有 1 分钟 bar 时，不写 5 分钟 bar，也不覆盖高低点时间。没有选"唯一键加 freq"：那样读分钟线的代码（alerts、views）会同时读到两种周期。
- 测试：东财 ×100、腾讯 amount→成交量且成交额为 None、新浪不变、分钟线单位和 source、1/5 分钟互斥。
- **运维须知**：
  - 迁移 0009 在 SQLite 上会**重建表**：dailyquote 重建 1 次，minutebar 重建 2 次（NOT NULL 加默认值时 Django 只能 remake table）。迁移前备份 db.sqlite3，并停掉 gunicorn/调度。分钟表很大时会耗一些时间和磁盘空间。
  - 存量行的 source 为 ''（来源未知），存量 BaoStock 5 分钟 bar 的 freq 会被默认标成 1，无法事后区分。
  - 修复存量单位：建议对自选股按单一源全量重拉一次日线：`python manage.py fetch_stock_data --all --full-daily --source sina`。
- 未做（不在我的文件范围）：探测器读取时的防御（相邻日量比 >50 倍判异常置 None），留给 detector 方向。

## DAT-02 前复权增量拼接（短期方案已修复，中期方案未实现）
- 新增 `_adjust_basis_changed`：增量拉取（含 `fetch_daily_summary` 的 3 天重叠）时，逐日比较重叠窗口里**今天以前**的新旧收盘价。偏差超过 `max(0.2% × 旧价, 0.006 元)` 就判定复权基准变化。绝对容差用来兜住库内 2 位小数的舍入误差。
- 判定变化后打 warning 日志，然后自动 `fetch_daily_data(full=True)`：从"库内最早日期"和"一年前"中较早的那个开始全量重拉。prev_close/change_pct 沿新数据链路全部重算，完成后再打 warning 日志。全量重拉失败时，**增量结果不落库**，下次增量还能再检测到并重试，避免新基准覆盖重叠窗口后检测失效。
- 区间内源端没有返回的库内行（如停牌缺口）保留旧值，日志会给出条数。
- `fetch_stock_data --full-daily [--source sina]` 可以手动全量重拉。
- 中期方案（只写说明，未实现）：入库改存**不复权**价，另建复权因子表（`adjust='hfq-factor'` 或 BaoStock 复权因子），展示和计算时现场复权；`change_pct` 改用交易所口径的"除权昨收"。这样除权不再需要回写历史。
- 测试：基准不变时不触发；3% 分红触发全量重拉、prev_close 链连续；舍入噪声不误判；全量失败时不写增量；命令单源全量重拉。
- **运维须知**：上线后对全部自选股跑一次 `fetch_stock_data --all --full-daily --source sina`，修复既有断层，同时统一单位（与 DAT-03 共用这一次）。之后可以再跑 `rebuild_high_low_times`。全量重拉只更新价格和量，不动高低点时间。

## DAT-07 交易日历（已修复）
- `is_trading_day` 改为三态 True/False/None：
  - 周末恒为 False。
  - 新浪整表带 TTL（6h）。刷新失败时沿用旧表，并且只在表覆盖的 [min, max] 范围内下结论。**范围外不再判为非交易日**，改为查 BaoStock 单日。
  - BaoStock 的单日结论带 TTL（6h）缓存。
  - 两个源失败后各自负缓存 300s，冷却期内不再请求，日历故障时不会逐日重复打上游。
  - 全部不可用时返回 None，不缓存猜测结果。原来"仅排除周末"的结果会被永久缓存，这条已删除。
- 默认日期改用 `timezone.localdate()`（原来用 UTC 日期）。
- 新增 `_reset_trade_calendar_cache()`，供测试和运维使用。
- services 内的调用方：盘中更新、收盘汇总遇到 None 保守跳过（写库类）；`fetch_stock_all_data` 改为 `is False` 才跳过，因为分钟线已经按日期过滤，日历未知时照常尝试。
- **需要其他方向知悉（行为变化）**：日历完全不可用时，原来工作日返回 True，现在返回 None。
  - `scheduler.py`（`if not is_trading_day()`）在日历全挂时会跳过写库任务。这符合报告"写库类遇 None 跳过"，但在新浪和 BaoStock 同时故障期间，个股数据会停更。
  - `market/_cache.py`、`snapshots._recent_trading_days` 等 `if is_trading_day(day):` 写法会把 None 当成非交易日，返回 None 或空。它们的 docstring 原本就写了"日历不可用时返回 None"。
  - `sentiment`、`snapshots` 中 `is False` 的写法不受影响。
  - 这些文件不归我，没有改。报告建议的"仓库内置年度静态交易日历作为最终兜底"和抽出 `stocks/calendar.py` 也没有做（属 ARC 条目）。
- 测试：命中与节假日；范围外返回 None；BaoStock 兜底及 TTL 过期后重查；全挂时逐日查询只打一次上游、冷却后恢复；刷新失败沿用旧表。

## 需要在 VPS 实测的点
1. 新浪 `stock_zh_a_minute` 的 volume 确实是"股"、amount 是"元"（报告和 akshare 文档都这么写，这里没有联网核对）；东财分钟线 `成交量` 是"手"。
2. 跑完迁移 0009 后执行 `rebuild_high_low_times --dry-run` 看更正规模，再正式执行。
3. 盘中观察日志：每股每轮写入的 bar 数应该是个位数，5 分钟周期内能跑完一轮。
4. 确认 VPS 的 Django ≥ 5.1，IMMEDIATE 才会生效。可以在 `manage.py shell` 里检查 `connection.settings_dict['OPTIONS']`。
5. 观察有没有"复权基准已变化"的告警频繁误触发。如果有，调大 `_ADJ_ABS_TOL`/`_ADJ_REL_TOL`。新浪和东财的 qfq 算法差异是否超出 0.2%，需要实测。

## 其他说明
- 在 worktree 里运行 `manage.py`（django.setup）会启动调度，并生成 `backend/db.sqlite3`、`scheduler.lock`。两个文件都已被 gitignore，本分支跑完后已删除。
