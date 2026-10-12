# StockTrace

个人用 A 股自选股监控面板 + 行情中心。后端 Django REST + SQLite，前端 Vue 3 + ECharts，部署在一台 1H2G 的小 VPS 上（Nginx + Gunicorn + systemd + Cloudflare，访问控制走 Nginx HTTP Basic Auth）。

## 功能

**自选监控**

- 按代码/名称搜索添加关注，添加即后台轻量拉取（不阻塞）
- 可折叠表格：实时价格、相对开盘的涨跌（`open_close_pct` 口径）
- 日 K 蜡烛图 + 当日分钟走势（局部极值检测，1% 振幅阈值），点击 K 线柱切换对应日期走势

**行情中心（`/market`）**

| 路径 | 说明 |
|------|------|
| `/market` | 数据首页：主要指数 + 宽基估值分位 + 北向/涨跌家数 + 涨停池情绪 + 两融余额 + 概念摘要 + 主力净流入 |
| `/market/trend` | 多指数归一化走势对比 |
| `/market/sectors` | 行业/概念板块资金轮动（当日横截面） |
| `/market/institutions` | 机构持仓（按股/按机构/北向/个股明细，可选 `?code=`） |
| `/market/national-etf` | 国家队相关 ETF 观察（研究名单，非官方持仓） |
| `/market/etf-radar` | ETF 份额规模、主力流入流出、涨跌榜 |

**探测器（`/detector`）**

**趋势延续信号**：在成交量处于相对平稳区间（平静判定：量比 0.7~1.5、量能波动收窄、振幅低位）时，监测「量比斜率 / 大单净流入 / 波动率压缩 / 换手率分位 / 融资余额 / 价格与均线关系」等维度的渐进变化；至少 2 个维度同向、连续维持 2 个周期以上、幅度温和才产出信号（分级 高/中/低）。验证显示信号通常出现在 ZigZag 波段已经转向之后，标的是**正在进行的趋势**，不是转折预判，也不是买卖建议；卡片附注信号日所处的趋势波段与天数。每交易日 17:15 自动扫描，并回看信号的命中率，自动微调维度权重与投票门槛（有界、全程留痕）；复盘给出 20/60 日节点，信号后前 10 个交易日不判失败。页面含信号列表（按严重程度筛选）、信号详情（各维度曲线）与板块微变热力图。

- **向上信号未经验证**：多轮前推验证中向上信号 20/60 日超额均不显著，页面标「未经验证」并排在向下信号之后；向下信号是经过验证的主口径。
- **融资拥挤度**（个股，只修饰不投票）：MB5 = T−5..T−1 融资买入额 ÷ T−1 融资余额，DTC = T−1 融资余额 ÷ 20 日均成交额，取截面分位。向上信号 MB5 分位 ≥0.70 时否决（被否决的信号单独列出），向下信号 MB5 或 DTC 分位 ≥0.70 时严重度升一档。MB5 截面为 T−1 全市场 A 股两融标的（交易所明细全量落库）；DTC 缺全市场成交额，本地日线样本不足 200 只时不参与，向下升级只看 MB5。两融为 T+1 披露，只用 T−1 及以前。
- **限售解禁**（个股，只提示不投票）：未来 20 个交易日内解禁合计 ≥ 流通股 1% 时在信号卡片与个股页提示（东财解禁日程），解禁日当天起失效。
- **日内追买过热**（个股，只展示标签）：ID20 = 近 20 日 ln(收盘/开盘) 之和的截面分位 ≥0.80 时标出；无全市场日线，本地样本不足 200 只时不取分位。
- 量价相关（vp_corr）维度经多轮验证无效，已删除。

**维度口径与已知边界**（数据诚信红线，不做合成数值）：

- 「换手率分位」：个股用真实换手率（成交量 / 新浪流通股本）的 60 日分位，送转、定增、解禁前后口径连续；取不到股本数据时退回成交量分位并标注「股本数据缺失」；指数无股本概念，用成交量分位作活跃度代理。
- 「融资余额/北向」仅融资余额口径（北向自 2024-08 停止逐日净买额披露）；融资余额为交易所 T+1 披露、经本站日度快照积累，上线初期约需一周凑齐窗口。
- 「委比与挂单结构」「板块内分化」两维度因无数据源**暂未启用**（前者需 Level-1 盘口数据，后者需板块成分股横截面，现有日度快照仅含板块净额与涨跌幅）；启用状态与原因在页面「维度与迭代状态」与 `/api/detector/config/` 中可见，待数据源接入后开启。
- 上游/快照不足的对象当天跳过并记录降级原因（`/api/detector/` 各响应 `meta` 与扫描日志），绝不伪造信号。

行情 API 统一返回 `meta`（来源、源数据日期、抓取时间、缓存状态、免责声明）；未知字段返回 `null`，不伪装为 `0`。

## 数据边界

- 板块资金是当日聚合强弱指标，不代表板块之间真实资金转移路径；当日横截面来自同花顺（东财板块接口对境外不可用），5/10/20 日轮动只由日度快照累加供数（2026-09 起积累中），窗口不齐时如实显示不可用。
- ETF 雷达的「股票/宽基范围」是可审计的名称规则筛选，不是基金官方分类；单只 ETF 详情只按需拉取价格历史，历史源失败时保留当前快照并明确提示。
- 国家队 ETF 页面是维护的研究观察名单，不提供无可审计来源的持仓主体、权重或成本。
- 机构数据来自公开季报/股东披露与北向统计，有披露滞后。
- 外部源失败一律报 unavailable/stale 并降级到下一源（源冷却 + 顺序 failover），绝不合成数值。

## 开发

运行时：Python 3.12（`.python-version`）、Node.js 22 LTS ≥ 22.13（`frontend/.nvmrc`）。

```bash
# 后端：在仓库根目录建虚拟环境（只需一次）
python3.12 -m venv venv                   # Windows：py -3.12 -m venv venv
source venv/bin/activate                  # Linux/macOS
# source venv/Scripts/activate            # Windows Git Bash
# .\venv\Scripts\Activate.ps1           # Windows PowerShell
pip install -r backend/requirements-dev.txt

cd backend
# 本地必须带 DJANGO_DEBUG=true：缺省 DEBUG=False，且此时没有合规的 DJANGO_SECRET_KEY 会拒绝启动
export DJANGO_DEBUG=true                  # PowerShell：$env:DJANGO_DEBUG="true"
python manage.py migrate
python manage.py runserver 8000           # 8000 被占用时换端口，并让前端 VITE_API_TARGET 指过来
python manage.py test stocks              # 全部后端测试（会访问上游的用例已打桩）
python -m ruff check .

# 前端（另开终端）
cd frontend
npm ci
npm run dev                               # http://localhost:5173，/api 代理到 localhost:8000
# VITE_API_TARGET=http://localhost:8001 npm run dev   # 后端不在 8000 时
npm run lint && npm test && npm run build
```

目录速览：`backend/stocks/`（models / datasources+ingest+jobs 多源抓取与入库 / tasks 后台拉取 / views/ 接口包 / market/ 行情中心包 / detector/ 探测器包 / scheduler 定时任务）；`frontend/src/`（views / components / api）；`deploy/`（VPS 部署模板与 `DEPLOY.md`）。

定时任务（APScheduler，交易日才跑；生产由独立进程 `python manage.py run_scheduler` 运行）：08:50 盘前增量日线；09–14 点每 5 分钟分钟线；15:10 收盘汇总；15:30–21:00 每 30 分钟预热晚到行情缓存；17:15 探测器日度扫描，当日快照未落库时 17:45/18:15/18:45 重试（可 `python manage.py run_detector_scan` 手动触发）；工作日 09:30 拉上一交易日个股融资明细（`stock_margin_daily`），每日 18:00 刷新限售解禁日程（`restricted_release`）。

## 部署

1H2G VPS：Nginx + Gunicorn（1 worker 2 线程）+ systemd，Cloudflare Full (strict) SSL + Nginx 限流 + Basic Auth。模板与步骤见 [`deploy/DEPLOY.md`](deploy/DEPLOY.md)。

**版本号以仓库根目录 `VERSION` 文件为唯一来源**（SemVer）：后端读入 `settings.APP_VERSION` 并在 `/api/health/` 返回 `version`，前端构建时注入并显示在页脚，`frontend/package.json` 的 `version` 与之保持一致（有单测校验）。发版流程：CI 通过后，1) 同步修改 `VERSION` 与 `frontend/package.json`、`package-lock.json` 的版本号，提交 `chore: 发布 vX.Y.Z`；2) 在 main 上打注释标签 `git tag -a vX.Y.Z`；3) `git push --follow-tags`。fix 升修订号，feat 升次版本号，破坏性变更升主版本号。
