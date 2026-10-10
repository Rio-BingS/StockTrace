# P0 修复说明：大盘资金流与快照回填（分支 fix/p0-market-flows）

## DAT-05 大盘资金流窗口口径混合 — 已修复（提交 5009c0a）
- **改法**
  - `snapshots.py` 抽出共用合并逻辑：`MARKET_FF_SECIDS=('1.000001','0.399001')`、`merge_market_ff_rows(by_secid)`（只合并沪深两边都有、且同日齐全的日期；某字段两边都为 None 时结果为 None，不再拿 0 补）、`fetch_market_ff_merged_rows()`（抓两个 secid，返回 `(合并行, 错误列表)`）。`save_daily_snapshots` 改为调用它（行为不变，payload 额外带 `src='em_fflow'`、`unit='元'`、`scope='sh+sz'`）。
  - `flows.get_market_fund_flow_window` 改用同一个 `fetch_market_ff_merged_rows()`：上游与快照口径一致。只有单边成功时，上游视为不可用，不覆盖快照里的两市合计，改用快照兜底。
  - 响应新增 `scope: 'sh+sz'` 和 `unit: '元'`。`meta.source` 由 `(1.000001)` 改为 `(沪深合计 1.000001+0.399001)`，disclaimer 也注明了口径和单位。
  - 窗口 items 改为合并行，不再带上证的 `close`/`change_pct`。前端 Market.vue 只读 `main_net`，不受影响。
- **单位**：`total_main_net` 等 `total_*` 仍是**元**，单位未变。数值从"仅沪市"变成"沪+深合计"，量级仍为 1e9~1e11。DAT-04 的前端显示由前端代理负责，本分支没有改。
- **测试**：`tests_p0_market_flows.MarketFlowWindowScopeTests`（3 个）。另外更新了 2 个原有测试的期望值，因为原 mock 对两个 secid 返回同一份数据，口径变了，期望值也要变：`tests.MarketWindowEndpointTests.test_market_flow_window_sums_and_drops_out_of_range`、`tests_features.MarketFundFlowSnapshotTests.test_window_snapshot_fills_missing_upstream_dates`。

## DAT-06（含 DS-01/DS-02/DS-04/DS-25）回填写入错误单位和口径 — 已修复（提交 14128c0）
- **DS-02 列名撞名**：`_normalize_market_fund_hist` 改用精确列名表 `_MARKET_FUND_NET_COLS`，只认 `主力/超大单/大单/中单/小单净流入-净额`，`-净占比` 列天然排除。rename 后如仍有重复列，保留首列并打 warning。测试用 akshare 实际返回的 15 列构造。
- **北向兜底源（stock_hsgt_hist_em，亿元）**：不再写入 `main_net`。换算成元（×1e8）后单列为 `north_net_buy`，此时 `main_net` 及各分项都是 None，返回 `available=False`、`scope='north'`，message 说明口径不同。前端 `v-if="mainHist.available"` 因此显示提示文字，不会把北向数据当主力画柱。主源返回值增加 `unit='元'` 和 `scope='sh+sz'`。新增常量 `MARKET_FUND_SOURCE_EM` / `MARKET_FUND_SOURCE_NORTH`。
- **backfill_market_snapshots 重写**
  - 只接受 `source == 'eastmoney_market_fund_flow'` 的结果，其他来源（北向兜底、多源失败）一律拒绝写库并打印原因。
  - 默认只补缺（`get_or_create`），`--force` 才覆盖。每行写入都包在 `transaction.atomic()` 里。
  - 只写 `<= _expected_latest_date()`（最近一个已完成的交易日）的日期。日历不可用时只写到昨天。日期统一用 `pd.to_datetime(...).date()` 处理。
  - payload 带 `src`/`unit`/`scope`。
  - **行业资金流回填已停用**（DS-04）：当日快照是同花顺口径，上游历史只有东财口径，名称和"主力"定义都不同。按规定不能新增 kind 或迁移，所以不回填，行业 20d 指标只靠收盘任务向前积累。`--sleep` 参数保留但不再生效，以免旧调用报错。
- **巡检**：`snapshots.audit_market_ff_snapshots()` 只读，命令入口为 `--audit`。判定原因如下：
  - `empty_payload`、`main_net_missing`
  - `magnitude`：0 < |main_net| < 1e6 元，疑似亿元口径
  - `untrusted_src`、`bad_unit`：payload 自带的标注与本表口径不符
  - `parts_all_none`：DS-02 的症状，只提示，不删
  `--audit --delete-suspect` 只删除前五类。
- **validate_market_sources** 新增"大盘主力资金流（口径/单位）"一节（只读）：输出 akshare 主源各分项的非空率，并把主源最近一个同日的 main_net 与自采沪深合计做比值对照（应约等于 1）。
- **测试**：`tests_p0_market_flows` 中的 `MarketFundHistNormalizeTests`（3 个）、`BackfillMarketFfTests`（4 个）、`AuditMarketFfTests`（3 个）。

## 测试结果
- 全量断网测试共 227 个（基线 214 + 新增 13），1 个失败，2 个错误：
  - 已知失败：`tests_features test_sina_fallback_when_em_fails`，日期写死在测试里，不在我的修复范围内。
  - 2 个 ERROR：`tests_detector` 中的 `test_all_sources_down_produces_no_fabricated_signals` 和 `test_calm_sector_with_two_aligned_dims_fires_signal`。把改动 stash 后在 HEAD 上同样报错，属于**原有问题，不是本分支引入**。原因是这两个测试依赖当前时刻：在 15:30 前运行时，`scan.run_scan()` 返回 `{'skipped': 'before_close'}`，没有 `status` 字段。我这次是在北京时间 00:xx 跑的。归 alerts-detector 方向处理。
- `ruff check .`：All checks passed。
- 运行时需要 `DJANGO_DEBUG=True`。settings 在没有 SECRET_KEY 时会拒绝启动，COMMON.md 里的命令没写这个变量。

## 运维须知（VPS）
1. 部署后先执行 `python manage.py backfill_market_snapshots --audit`，确认有没有污染行。目前判断回填从未成功执行过，预计为 0 行。
2. 若有 `magnitude`/`untrusted_src` 行：执行 `--audit --delete-suspect`，然后在东财恢复后运行 `python manage.py backfill_market_snapshots` 补缺。
3. 东财恢复后，先跑 `python manage.py validate_market_sources`，看"大盘主力资金流"一节的比值是否约等于 1，确认 akshare `secid+secid2` 的结果确实等于两市合计、单位为元。确认后再跑回填。
4. 旧的 market_ff 行没有 `src`/`unit`/`scope` 字段，读取方不依赖这些字段，不用迁移。

## 未完成 / 需要在 VPS 实测
- 「akshare `stock_market_fund_flow` 等于自采沪深合计」这一假设来自审查报告，本地断网无法验证，需要在 VPS 上用 validate_market_sources 实测比值。
- 北向兜底 `当日成交净买额` 的单位是否为亿元，依据是审查报告引用的 akshare 源码（/100），没有在线实测。
- `etf_flow.py` 没有改（`parse_flow_kline` 原样输出元，符合预期）。
- 在不新增 kind 的前提下，行业回填无法做到口径一致，因此直接停用。如果以后要恢复，需要 data-ingest 方向新增 kind（如 `industry_ff_em`）和迁移。
