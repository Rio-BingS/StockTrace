> ⚠️ 已过时：v1.01 时代的交接文档，仅供考古。其中的部署方式（Flexible SSL、http 访问等）已废弃，以 `deploy/DEPLOY.md` 与 `CLAUDE.md` 为准。

# StockTrace 项目交接文档

## 概要

StockTrace 是一个全栈 A 股监控面板（v1.01），后端 Django 6 + DRF，前端 Vue 3 + ECharts。功能包括：支持按名称/代码搜索添加关注、可折叠行表格显示实时价格及涨跌数据、日 K 线蜡烛图、当日分钟走势图（含峰谷检测及开盘/收盘/最高/最低/现价标注和百分比差值）、点击日 K 线柱可查看对应日期走势图。

应用已部署至 VPS（Ubuntu 22.04，1H2G），地址 http://stocktrace.yumeat.cc.cd，采用 Nginx + Gunicorn + systemd 架构，Cloudflare CDN（Flexible SSL）提供 HTTPS。Nginx 已配置 HTTP Basic Auth 访问控制。

## 修改文件清单

**后端：**
- `backend/stocks/models.py` — DailyQuote 新增 `prev_close`、`change_diff`、`change_pct` 字段；更新 `compute_derived_fields()`
- `backend/stocks/services.py` — 新增 `search_stocks()`（带缓存 A 股列表）；`fetch_daily_data()` 按日期排序遍历计算昨收/涨跌幅，从数据库兜底获取最早日的前收盘；`fetch_stock_all_data()` 开头加名称补全逻辑；`_try_sources` 退避上限从 4s 降到 2s
- `backend/stocks/views.py` — StockViewSet 新增 `search` action；`dashboard` view 返回新字段；`create()` 和 `perform_create()` 移除同步 `fetch_stock_info` 调用，name 为空时用 code 暂代（后续由数据拉取补全）
- `backend/stocks/serializers.py` — 新增 `StockSearchSerializer`；`DashboardStockSerializer` 扩展 prev_close/change 字段；`DailyQuoteSerializer` 字段更新
- `backend/StockTrace/settings.py` — SECRET_KEY/DEBUG/ALLOWED_HOSTS 从环境变量读取；CORS 限制为指定域名；添加 STATIC_ROOT；移除 `CsrfViewMiddleware`；DRF `DEFAULT_AUTHENTICATION_CLASSES` 设为空、`DEFAULT_PERMISSION_CLASSES` 设为 `AllowAny`
- `backend/stocks/urls.py` — 未改动（现有路由覆盖 search action）
- `backend/stocks/migrations/0003_*.py` — 自动生成迁移，DailyQuote 新增 3 字段
- `backend/requirements.txt` — 新建：锁定 Python 依赖版本

**前端：**
- `frontend/src/components/StockTable.vue` — 重写：可折叠行（基本信息折叠，点击展开振幅/OHLC），折叠和展开状态均有 K 线入口按钮
- `frontend/src/components/IntradayChart.vue` — 新建：ECharts 分钟走势折线图，MarkLine/MarkPoint 标注，局部极值检测（1% 阈值），下方数据表格
- `frontend/src/components/KlineChart.vue` — 新增 `date-click` 事件（点击 K 线柱触发），缓存 sortedData ref
- `frontend/src/components/StockWatchlist.vue` — 重写：统一搜索框 + 防抖下拉，支持代码或名称搜索
- `frontend/src/views/StockDetail.vue` — 集成 IntradayChart，`showIntraday(date)` 切换日期，`onKlineDateClick` 处理函数，响应式布局
- `frontend/src/views/Dashboard.vue` — 小改：使用更新后的可折叠 StockTable

**部署相关：**
- `.gitignore` — 新建：排除数据库、虚拟环境、node_modules、dist、.env
- VPS: `/etc/stocktrace/env` — 环境变量文件（SECRET_KEY、DEBUG=False、ALLOWED_HOSTS）
- VPS: `/opt/stocktrace/backend/gunicorn_config.py` — Gunicorn 配置（绑定 127.0.0.1:8000，2 个 worker，无 user/group 指令）
- VPS: `/etc/systemd/system/stocktrace.service` — systemd 服务（User=www、EnvironmentFile、Restart=always）
- VPS: `/etc/nginx/sites-available/stocktrace` — Nginx 站点配置（前端 dist + API 反代 + 静态文件）

## 关键决策

1. **涨跌指标选择**：用户明确否定相对昨收的涨跌幅，前端使用 `open_close_pct`/`open_close_diff`（相对当日开盘）。`prev_close`/`change_pct`/`change_diff` 保留在数据库中但不在前端展示。
2. **局部极值阈值**：设为全天振幅的 1%（用户否定了 5%，认为对 A 股来说太大）。
3. **Gunicorn 用户冲突**：systemd 的 `User=www` 不能与 Gunicorn 配置的 `user=` 同时存在——systemd 已经降权，Gunicorn 再次调用 setuid 会报 Operation not permitted 错误。
4. **VPS 上升级 Python 3.12**：Django 6.0 要求 Python >=3.12，Ubuntu 22.04 默认 3.10。通过 deadsnakes PPA 安装 3.12 而非降级 Django（面向未来、零代码改动）。
5. **VPS 上升级 Node.js 22**：Vite 8 要求 Node >=20.19，Ubuntu 22.04 默认 v12。通过 `n 22` 升级。
6. **Cloudflare Flexible SSL**：选此方案而非 Certbot，更简单——Cloudflare 负责用户端 HTTPS，HTTP 回源到 VPS。
7. **HTTP Basic Auth 做访问控制**：在 1H2G VPS 上选择 Nginx Basic Auth 而非 Django 认证系统，资源占用最小、实现最简单。
8. **添加股票先保存后补名称**：`perform_create` 中不再同步调用 `fetch_stock_info`（VPS 上外部数据源太慢，导致前端 30s 超时显示"添加失败"），name 为空时用代码暂代，由 `fetch_stock_all_data` 在数据拉取时异步补全。
9. **DRF 关闭 CSRF 和认证**：Nginx Basic Auth 已做访问控制，Django 层不需要。移除 `CsrfViewMiddleware`，DRF `DEFAULT_AUTHENTICATION_CLASSES` 设为空、`DEFAULT_PERMISSION_CLASSES` 设为 `AllowAny`，避免 CSRF 403 拦截 POST 请求。

## 已知问题

（当前无已知问题）

## 已完成部署任务

1. ~~HTTP Basic Auth~~ — Nginx 上已配置 `auth_basic` + `.htpasswd`
2. ~~Cloudflare 代理~~ — 已开启橙色云朵，SSL/TLS 模式设为 Flexible
3. ~~浏览器 HTTPS / HSTS~~ — Cloudflare Flexible SSL 已正确配置，浏览器可正常通过 HTTPS 访问

## 当前版本 v1.01 修复记录

**Bug 1：添加股票超时失败** — `perform_create` 同步调用 `fetch_stock_info` 获取股票名称，VPS 上外部数据源（东方财富/新浪/BaoStock）太慢，前端 30s 超时显示"添加失败"。

修复：
- `views.py`：`create()` 和 `perform_create()` 移除同步 `fetch_stock_info`，name 为空时用代码暂代，立即返回
- `services.py`：`fetch_stock_all_data()` 开头加名称补全（检测 `stock.name == stock.code` 时调 `fetch_stock_info` 补全）；`_try_sources` 退避上限从 4s 降到 2s

**Bug 2：添加股票 403 CSRF 校验失败** — 加 Nginx Basic Auth 后，浏览器认证弹窗打断了 Django 设置 CSRF cookie 的流程，前端 POST 无 `csrftoken` cookie。DRF 默认 `SessionAuthentication` 和 Django `CsrfViewMiddleware` 双重拦截导致 400/403。

修复：
- `settings.py`：移除 `CsrfViewMiddleware`；DRF `DEFAULT_AUTHENTICATION_CLASSES` 设为空、`DEFAULT_PERMISSION_CLASSES` 设为 `AllowAny`
