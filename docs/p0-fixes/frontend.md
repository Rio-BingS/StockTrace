# P0 修复说明（前端）— 分支 fix/p0-frontend

只改了 `frontend/` 下的文件，没动后端、models 和迁移。

## 校验结果
- `npm run lint`：0 问题
- `npm run build`：通过
- `npm test`（新增，Node 内置 `node:test`，**没加任何新依赖**）：7/7 通过
- 后端：没有改动，所以没跑 Django 测试（基线不受影响）

## FEF-01 涨跌幅/涨跌价改成相对昨收 — 已修复
- 先核对了字段：`DailyQuoteSerializer`（日线列表 / daily_latest）和 `DashboardStockSerializer`（看板）都返回 `prev_close / change_diff / change_pct`。`change_pct` 已经是百分数（模型 `change_diff / prev_close * 100`）。分钟线 `MinuteBarSerializer` **不返回 prev_close**，分时图的昨收改从传进来的 `dailyQuote.prev_close`（当日日线）取。
- `utils/format.js`：新增 `formatSigned`（涨跌额带正负号）和 `pctFrom(price, base)`（基准缺失或为 0 时返回 null，显示为 "-"）。
- `components/StockTable.vue`：现价颜色、涨跌幅、涨跌价改用 `change_pct / change_diff`。展开行里新增"日内涨幅（较开盘）"和"昨收"，原来的日内口径保留，但标明了基准。
- `views/StockDetail.vue`：摘要卡的涨跌幅、涨跌价改用 `change_*`，另外新增"昨收"和"日内涨幅（较开盘）"两项。
- `components/KlineChart.vue` tooltip：涨跌幅优先取 `change_pct`；为 null 时用 `prev_close` 算，再没有就用前一根 K 线的收盘价算，都没有就显示 "-"，不会回退到开盘口径。tooltip 里另外列出"日内涨幅（较开盘）"。
- `components/IntradayChart.vue`：
  - 百分比基准改为昨收，拿不到昨收时才回退到开盘价。标题下会写明"百分比较昨收 / 较开盘"，tooltip 和阶段高低点表头也标了基准。
  - 有昨收时画一条实线"昨收"，y 轴 min/max 会把昨收包进去，保证这条线看得见。
  - 表格里现价颜色和涨跌幅改用 `change_pct`，并新增"昨收"和"日内涨幅（较开盘）"。
  - 顺手修了 FEF-13 的写死 "+" 号：所有标注统一走 `formatPct`，不会再出现 "+-1.23%"。
- 测试：`src/utils/__tests__/format.test.js`（高开低走场景下较昨收与较开盘的方向、null 基准、formatSigned）。

## FEF-02 两融余额区块永不加载 — 已修复
- `views/StockDetail.vue`：去掉 `onMounted` 和 `watch(stockId)` 里对 `loadMargin()` 的直接调用，改为 `watch(code, c => c ? loadMargin() : margin=null)`。另外 `loadMargin` 加了竞态保护：切换股票后，旧请求晚到会被丢弃。
- 测试：项目没有组件挂载测试框架（没装 Vitest 或 @vue/test-utils），所以没写自动化测试。手工验证步骤：打开 `/stock/<id>`，Network 里应出现 `getStockMargin(<code>)` 请求，页面底部显示"两融余额（融资融券）"区块；从详情页切到另一只股票的详情页，应再发一次请求，区块数据随之更新。

## DAT-04 大盘"主力合计"把元当亿显示 — 已修复
- `utils/marketFormat.js`：`formatAmount(val, { signed, digits })` 作为统一的金额格式化函数（输入单位是元，输出带 万 / 亿 / 万亿 后缀）。
  - 先按目标精度四舍五入再选单位，同时修了 FE-17 的边界问题：99,999,999.9 → "1.00 亿"，9,999.6 → "1.00 万"。
  - `signed: true` 时正数加 "+"。
  - 不传第二个参数时行为和原来一致，原有 37 处调用无需改动。
- 新增 `formatYiAxis`，供坐标轴使用（元→亿，取整，例如 "-52亿"）。
- `views/Market.vue`：
  - "主力合计"改为 `formatAmount(total_main_net, { signed: true })`。
  - 柱状图 y 轴加了 `formatYiAxis`，tooltip 加了 `valueFormatter: formatAmount`。
  - **判断点**：柱状图在窗口接口失败时会回退到 `fund.main_hist`。这个数据源在北向兜底（`source === 'eastmoney_hsgt_hist_north'`，akshare `stock_hsgt_hist_em` 净买额）时单位是**亿元**，所以前端按 source 乘 1e8 统一成元再作图。更彻底的做法是后端在 `_normalize_market_fund_hist` 里统一单位（不在本分支范围，建议 data/market 方向处理）。
- 测试：`src/utils/__tests__/marketFormat.test.js`（元→亿、正负号、边界进位、空值、坐标轴格式）。
- `package.json` 新增 `"test": "node --test \"src/**/*.test.js\""`（Node ≥ 20 支持 glob，当前环境是 Node 22）。

## 未完成 / 需在 VPS 实测
- DAT-04：要在页面上实测一次"主力合计"的量级，正常应是几十到几千亿；同时确认北向兜底源的单位确实是亿元，如果不是，就删掉 `factor`。
- `Market.vue:260` 北向的 `formatYi(northNet) + ' 亿'` 和 `:351` 的 `formatYi(row.net_buy)` 没改。这两个值的单位不在本次 P0 范围，没有核实，建议在 FEF-11 统一格式化时一起核对。
- 分时图：`intradayQuote` 在 `dailyData` 里找不到该日时会回退成 `latestQuote`，那时昨收可能对不上当天（属于原有逻辑，没改）。另外，如果某天 `prev_close` 为空（比如首个交易日），会按开盘口径显示，并标注"较开盘"。
- 合并提示：本分支改了 `frontend/package.json`（新增 test 脚本）和 `utils/format.js`、`utils/marketFormat.js`，如果其他分支也动了这些文件，注意冲突。
- 本地 `frontend/node_modules` 是指向 `work/fe/node_modules` 的软链（已被 .gitignore 忽略，没提交），`dist/` 也没提交。
