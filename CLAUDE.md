# CLAUDE.md

给 AI 助手的工程入口：读这一份即可上手。历史细节在 git log 与 PROJECT_PROGRESS.md，不要凭记忆猜测本文件的规则。

## 项目是什么

StockTrace：个人 A 股自选监控 + 行情中心。Django REST + SQLite（`backend/`），Vue 3 + ECharts（`frontend/`），手动部署在 1H2G VPS（Nginx + Gunicorn + systemd + Cloudflare + Basic Auth）。

**权威代码只有根目录 `backend/` 与 `frontend/`**；`StockTrace v*/` 是退役的目录快照（已 gitignore，本地归档）；`venv/`、`node_modules/` 不入库。

## 文档地图

| 文件 | 内容 | 何时读 |
|---|---|---|
| `CLAUDE.md`（本文件） | 命令、架构、不变量、红线 | 总是（自动加载） |
| `PROJECT_PROGRESS.md` | 当前状态、**待办与已知问题**、极简历史 | 接手任务前 |
| `README.md` | 面向人的功能介绍 | 向用户解释项目时 |
| `deploy/DEPLOY.md` | VPS 部署步骤与模板 | 部署/换机时 |
| `docs/archive/handoff*.md` | v1.01 时代交接（已过时，仅考古） | 一般不用 |

## 命令

```bash
source venv/Scripts/activate            # Windows Git Bash
cd backend
# 本地必须带 DJANGO_DEBUG=true：settings 缺省 DEBUG=False，且 DEBUG=False 时缺
# DJANGO_SECRET_KEY 会拒绝启动（生产安全缺省，勿改回）
DJANGO_DEBUG=true python manage.py runserver 8001  # 本地 8000 常被占用；前端用 VITE_API_TARGET 指向对应端口
DJANGO_DEBUG=true python manage.py test stocks     # 全部测试（调度器自动跳过）
DJANGO_DEBUG=true python manage.py validate_market_sources  # 只读上游字段/数据源审计，不写库
python -m ruff check .                  # lint（ruff.toml：仅 E4/E7/E9/F）
DJANGO_DEBUG=true python manage.py fetch_stock_data --all # 手动拉自选行情（默认 light，full=1 更全）
DJANGO_DEBUG=true python manage.py run_detector_scan     # 手动触发探测器扫描（幂等；非交易日/收盘前如实跳过）

cd frontend
npm run dev        # 5173，/api 代理到 localhost:8000（VITE_API_TARGET 可覆盖）
npm run lint && npm run build   # eslint（vue essential 档）+ 产物到 dist/
```

## 架构（改哪里）

| 层 | 位置 |
|---|---|
| 模型 | `backend/stocks/models.py`（Stock / DailyQuote / MinuteBar / MarketDailySnapshot / PriceAlert / DetectorSignal 等） |
| 自选行情抓取（多源降级） | `backend/stocks/services.py`（EastMoney→Sina→BaoStock→Tencent） |
| 后台拉取线程 | `backend/stocks/tasks.py`（异步 + fetch-status 轮询） |
| 行情中心 | `backend/stocks/market/` 包：`_cache`（TTL+交易日历保鲜）/`_sources`（冷却+failover）/`_query`/`periods`（**统一区间解析**）+ `indices`/`flows`/`sectors`/`etf`/`etf_flow`/`institutions`/`sentiment`（涨停池情绪/沪深两融汇总）/`margin_stock`（个股两融明细）/`snapshots`（日度快照+自检）/`holders`（定报持有人结构）/`overview` |
| 探测器（一级模块） | `backend/stocks/detector/` 包：`registry`（维度注册表，单一事实来源）/`dimensions`（维度计算+序列构建）/`calm`（平静判定闸门）/`signals`（同向投票+周期+分级）/`review`（5日回看→权重/阈值有界迭代）/`scan`（编排+降级留痕）/`views`；平静盘面监测渐进微变，17:15 日度扫描 |
| 调度器 | `backend/stocks/scheduler.py`（APScheduler + 文件锁单实例） |
| REST + 页面 | `backend/stocks/views.py` `urls.py`；前端 `frontend/src/views/` `components/` |
| 前端 API 封装 | `frontend/src/api/stocks.js`（axios baseURL `/api`） |

## 行情 API 与区间体系

区间档位：`1d/3d/5d/1w/1m/3m/6m/1y/ytd` + `?start=&end=` 自定义（`periods.py` 统一解析，非法值 400）。**各数据只支持适合它的档位，接口会校验并如实标注**：

| 端点 | 区间能力 | 数据深度/边界 |
|---|---|---|
| `market/trend/` | period（交易日 5/22/66/130/260）+ 旧 `days=` + 自定义≤3年 | 新浪日线，深度足够 |
| `market/sectors/?board=&period=` | `day/5d/10d/20d`（**无 3d**） | 当日=同花顺自实现抓取（东财板块路径对境外 502）；5d/10d 快照优先、东财原生排行兜底；20d 仅快照供数 |
| `market/national-etf/flow/?period=` 或 start/end | `1d/3d/5d` 按交易日 + `1w~6m/ytd` 自然日 + 自定义 | push2his 约 120 交易日（境外被掐时 push2delay 兜底仅当日一根），超出标 `coverage_start`+`truncated` |
| `market/market-flow/` | 全档 + 自定义 | 同上（secid 1.000001 大盘；另有日度快照兜底） |
| `market/northbound/` | 全档 + 自定义 | 北向净买额历史切片 |
| `market/etfs/<code>/?range=` | `1w/1m/3m/6m/1y` | 价格历史，历史源失败保留现价快照 |
| `market/institutions/?quarter=` | 季度（`2026Q1`） | 季报，披露滞后 |
| `market/stock-margin/<code>/` | 当日点值 | 个股两融明细，T+1 披露；非两融标的如实降级 |
| `market/`、`etf-radar/`、`national-etf/` | 当日快照 | 份额变化与 5/10/20 日轮动**需日度快照积累（二期，勿伪造）** |

所有行情响应带 `meta`（available/source/source_data_date/data_as_of/fetched_at/cache_status/disclaimer）；缓存是交易日历感知的：非交易时段只要缓存已覆盖最近已完成交易日收盘就算新鲜，不重拉上游。

## 探测器口径与边界（改 detector/ 前必读）

- **数据诚信**：维度只用真实上游/本站快照推导；`委比与挂单结构`（无盘口数据源）、`板块内分化`（快照缺成分股横截面）两维度在 `registry.py` 显式 `enabled=False` 并写明原因，**不得用代理值冒充**，接入数据源后再开启。
- **换手率分位**＝成交量自身 60 日分位：个股窗口内股本不变时与换手率分位单调等价（口径已在 README/API note 标注）；指数为活跃度代理。
- **融资余额**：交易所 T+1 披露，`MarketDailySnapshot` 新 kind `margin` 由探测器扫描落库（只写沪深同日齐全行）；北向自 2024-08 停止逐日披露，维度如实仅融资余额口径。
- **平静参数不参与自动迭代**：review 只调维度权重 [0.5, 2.0] 与投票门槛 min（[0.5×, 2×] 默认值）；调平静闸门会让扫描宇宙随命中率漂移（正反馈失控），只能人工改。
- 信号 `trade_date` 跟随数据自身日期（`ctx.as_of`），上游晚到不冒充扫描当天；扫描幂等（信号/run 行按日期覆盖），回看只处理 `review_hit` 为 null 的行。

## 不变量与红线（违反=事故）

1. **数据诚信**：外部源失败→报 unavailable/stale 并降级下一源，**绝不合成数值；未知= `null`，不是 0**；没有历史数据的功能明确标"待积累"，不做伪造。
2. **安全模型**：DRF AllowAny + 无 CSRF，访问控制全靠 Nginx Basic Auth（settings.py 有红线注释）。8000/8001 绝不对公网暴露；systemd `User=www` 下 **Gunicorn 禁止设 user=**。
3. **1H2G 红线**：Gunicorn 1 worker 2 线程；不要加 worker；自选 ≤ ~20 只；前端 60s 轮询。
4. **发版 = git 标签**：验证通过后 `git tag -a vX.Y.Z`（SemVer，当前 v1.2.1）+ `git push --follow-tags`；不再维护目录快照。
5. **密钥绝不入库**：`.env`、`地址.md`、db、scheduler.lock 已 gitignore；推送前扫描。
6. **本地环境坑**：本机代理对 `push2his.eastmoney.com` 的 TLS 间歇性干扰——资金流抓取已内置 https 退避重试 + http 兜底（故意不走 akshare 的同源封装）；2026-09-07 诊断确认 VPS（境外）对该域名同样整体不可达，fflow 已加 push2delay 镜像兜底（仅当日一根）。akshare 版本已在 requirements.txt 钉死，勿随手升级；8000 端口被用户其他服务占用，后端用 8001。
7. **选源原则：交易所官方优先**：披露类数据（ETF 份额、两融、收盘行情口径等）交易所官方接口可用时优先于商业聚合源（东财/乐咕），聚合源兜底；衍生类数据（主力资金流、涨停池、概念归类、估值分位）交易所无对应披露，只能用商业源。两源同用时如实标注口径与披露时点（如两融 T+1、深市晚于沪市），不混用不同日期的数据硬凑合计。

## 提交规范

Conventional Commits 中文（`feat:`/`fix:`/`docs:`/`chore:`/`refactor:`/`perf:`），一次提交一件事，≤50 字标题，push 前 `git pull --rebase`。细节见用户全局规范。

## 验证清单（任何后端改动）

```bash
cd backend  && python -m ruff check . && python manage.py test stocks   # 全部测试
cd frontend && npm run lint && npm run build
```
