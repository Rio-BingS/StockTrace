# StockTrace v1.02 部署说明

目标环境：Ubuntu 22.04，**1H2G** VPS，路径 `/opt/stocktrace`，Nginx + Gunicorn + systemd，Cloudflare Full (strict) SSL（回源加密 + Nginx 限流，见第 5 节），HTTP Basic Auth。

本目录为**发布快照**（与仓库根目录 `backend/`、`frontend/` 同步的一份拷贝）。日常开发改根目录；部署时把本快照或 git 同步到 VPS。

---

## 0. 相对 v1.01 / 线上旧版的变更

### Bug

- 看板「涨跌价」不再误加 `%`
- 日 K 成交量柱（注册 ECharts `BarChart`）
- 详情路由换 `id` 会重新加载
- Gunicorn 下 APScheduler：文件锁单实例 + 不再只靠 `RUN_MAIN`

### 功能

- 拉取改为**后台线程**，接口立即返回；`GET /api/stocks/fetch-status/` 可轮询
- 添加/恢复自选后**自动轻量拉行情**（补名称 + 增量日线 + 近 2 日分钟）
- 前端 `utils/format.js` 统一格式化
- **市场数据一级页「数据」** + 二级专题（需前端重新 build，Nginx 已配置 SPA `try_files`）：
  - `/market` 首页（指数 + 北向/情绪摘要）
  - `/market/trend` 全市场走势
  - `/market/sectors` 板块资金轮动
  - `/market/institutions` 机构持仓（季报/股东变动/北向；`?code=` 个股明细）
  - `/market/national-etf` 国家队相关 ETF 观察（研究名单，非官方持仓）
  - `/market/etf-radar` ETF 份额雷达
  - API：`GET /api/market/`、`/api/market/trend|sectors|institutions|national-etf|etf-radar/`
  - 单只 ETF：`GET /api/market/etfs/<code>/?range=1m|3m|6m|1y`
- 市场外网：源冷却 + 多源 failover（主力资金历史可降级北向序列）

### 1H2G 性能

- Dashboard **2 次查询**拼最新行情（去掉 N+1）
- 日线按「最近一根 bar + 重叠」增量；无历史才 bootstrap ~365 天
- 添加后 / 手动刷新默认 **light**（少分钟）；`full=1` 才更全
- 调度：**08:50** 增量日线 · **09–14 */5** 分钟 · **15:10** 收盘 · **15:30–21:00 */30** 晚到行情缓存预热（仅交易日，不写新的市场历史表）
- 交易日日历 / A 股列表缓存；名称优先 BaoStock/新浪（避免东财全市场快照）
- Gunicorn 默认 **1 worker × 2 threads**（`gunicorn_config.py`）
- 看板轮询 **60s**；拉取状态轮询 3s
- 详情页用 `GET /stocks/:id/` 不再拉整表

---

## 1. 首次部署（概要）

```bash
# 系统依赖（示例）
sudo apt update
sudo apt install -y python3.12 python3.12-venv nginx apache2-utils

# 代码放到 /opt/stocktrace （本快照的 backend + frontend）
sudo mkdir -p /opt/stocktrace /etc/stocktrace /opt/stocktrace/staticfiles
sudo chown -R www:www /opt/stocktrace

# 虚拟环境（放在 backend/venv，与线上保持一致）
cd /opt/stocktrace
python3.12 -m venv backend/venv
source backend/venv/bin/activate
pip install -r backend/requirements.lock   # 锁文件，全量精确版本（OPS-02）

# 环境变量
sudo cp deploy/env.example /etc/stocktrace/env
sudo nano /etc/stocktrace/env   # 填 SECRET_KEY、ALLOWED_HOSTS
# 可选：STOCKTRACE_PUSH_URL（Server酱兼容；价格提醒 + 快照自检告警推送，
# 未配置则静默跳过）、MARKET_SNAPSHOT_RETENTION_DAYS（快照保留天数，默认 600）

# 数据库
cd /opt/stocktrace/backend
source ../backend/venv/bin/activate
# 环境变量只对 systemd 进程自动生效，手动跑 manage.py 必须先加载：
set -a; source /etc/stocktrace/env; set +a
python manage.py migrate
python manage.py collectstatic --noinput

# 前端构建（需 Node >= 20）
cd /opt/stocktrace/frontend
npm ci
npm run build

# systemd：Web（gunicorn）与调度器（run_scheduler）是两个独立单元（REL-01）
sudo cp deploy/stocktrace.service deploy/stocktrace-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stocktrace stocktrace-scheduler

# Nginx + Basic Auth
sudo htpasswd -c /etc/nginx/.htpasswd_stocktrace stocktrace
sudo cp deploy/nginx-stocktrace.conf /etc/nginx/sites-available/stocktrace
sudo ln -sf /etc/nginx/sites-available/stocktrace /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

注意：

- **Gunicorn 配置不要设 `user=`**，systemd 已是 `User=www`
- Cloudflare SSL 模式：**Full (strict)**，需先配源站证书，见第 5 节
- 域名改到你自己的 `server_name`

---

## 2. 从旧版升级到 v1.02

```bash
# 备份（升级前务必先做一次在线一致性备份，见第 6 节；不要直接 cp，WAL 模式下会丢数据）
sudo -u www /opt/stocktrace/deploy/stocktrace-backup.sh
sudo systemctl stop stocktrace

# 同步代码（按你习惯：scp / git / tar）
# 保证 gunicorn_config.py、stocks/tasks.py、scheduler.py 等到位
#
# ⚠️ root 身份 tar 解包会把 backend/、frontend/ 目录属主重置为 root，
#    导致 www 无法在目录里创建 SQLite journal 文件 → 所有查询报
#    「attempt to write a readonly database」，且被各处容错 except 吞掉、
#    页面表现为莫名「数据暂不可用」。同步后必须执行：
#      chown -R www:www /opt/stocktrace

cd /opt/stocktrace
source backend/venv/bin/activate
pip install -r backend/requirements.lock   # 锁文件，全量精确版本（OPS-02）
# ⚠️ 一律装 requirements.lock，不要装 requirements.txt、不要 pip install -U（见第 7 节）。
# ⚠️ akshare 版本已钉死（==1.18.64）：其接口频繁变动，
#    同花顺板块资金流解析由 stocks/market/sectors.py 自实现——勿随手
#    pip install -U akshare（2026-09 曾因静默升级破坏解析导致快照断供）。
cd backend
set -a; source /etc/stocktrace/env; set +a   # 手动 manage.py 必须加载环境变量
python manage.py test stocks
python manage.py migrate
python manage.py collectstatic --noinput

cd /opt/stocktrace/frontend
npm ci && npm run build

# 同步 systemd 单元（Web + 独立调度进程）；/etc/stocktrace/env 里若有
# STOCKTRACE_RUN_SCHEDULER_IN_WEB=1 要删掉，否则 Web 会继续在进程内跑调度
sudo cp /opt/stocktrace/deploy/stocktrace.service /opt/stocktrace/deploy/stocktrace-scheduler.service /etc/systemd/system/
sudo cp /opt/stocktrace/deploy/gunicorn_config.py /opt/stocktrace/backend/gunicorn_config.py   # 若两份不同
sudo systemctl daemon-reload
sudo systemctl enable stocktrace-scheduler
sudo systemctl start stocktrace stocktrace-scheduler

# 确认调度只在独立进程里启动一次、Web 里没有
sudo journalctl -u stocktrace-scheduler -n 50 --no-pager | grep -i APScheduler
sudo journalctl -u stocktrace -n 200 --no-pager | grep -i APScheduler    # 应无输出
curl -s http://127.0.0.1:8000/api/health/ | python3 -m json.tool
```

期望：`stocktrace-scheduler` 日志里 `APScheduler 已启动 ...，独立进程 pid=NNN` 出现一次；
`/api/health/` 返回 `"status": "ok"`、`scheduler.alive: true`、各 job 的 `next_run_time`。

---

## 3. 运维命令

```bash
sudo systemctl status stocktrace
sudo systemctl restart stocktrace
sudo journalctl -u stocktrace -f

# 手动拉数（在 venv 内；先加载环境变量）
cd /opt/stocktrace/backend
set -a; source /etc/stocktrace/env; set +a
python manage.py fetch_stock_data --all

# 只读校验 ETF/板块上游字段与单位；不写数据库、不修改缓存
python manage.py validate_market_sources

# 调度器（独立进程，REL-01）
sudo systemctl status stocktrace-scheduler
sudo journalctl -u stocktrace-scheduler -f
# 临时关闭调度：直接停调度单元（Web 不受影响）
sudo systemctl stop stocktrace-scheduler
# 或在 /etc/stocktrace/env 加 STOCKTRACE_SCHEDULER=0（全局总开关，run_scheduler 会直接退出）
```

### 调度器运行方式说明（REL-01）

- 调度器不再寄生在 Gunicorn worker 里：worker 被 `max_requests` 回收或重启时会在
  `graceful_timeout` 后强杀进行中的任务，并让落在重启窗口里的 15:10 / 17:15 定时点被判 misfire 跳过。
- 现在由 `python manage.py run_scheduler`（`stocktrace-scheduler.service`）前台运行；停止时 SIGTERM，
  等正在执行的任务结束再退出（`TimeoutStopSec=300`）。重启 Web（发版、改 nginx）不再影响调度。
- 所有任务显式 `timezone=Asia/Shanghai`、`coalesce=True`、`max_instances=1`，并设置 misfire 宽限
  （盘中 5 分钟任务 240s，08:50 预热 1800s，15:10 汇总 / 17:15 探测器 3600s，收盘后预热 900s）。
  宽限内晚到的任务仍会执行（各任务自带交易日 / 收盘时间守卫）。
- 锁文件与心跳文件在 `/run/stocktrace/`（`RuntimeDirectory=`，属主 www，开机清空，不会残留 root 属主的锁）。
  拿不到锁时 run_scheduler 每 60s 重试并记 WARNING。
- 兼容旧部署：`STOCKTRACE_RUN_SCHEDULER_IN_WEB=1` 时仍在 Web 进程内启动（同一把锁，二者不会同时跑）。
  本地开发 `runserver` 想要定时任务时也用这个变量，或另开终端跑 `python manage.py run_scheduler`。
- Gunicorn `max_requests` 由 500 调到 20000（`GUNICORN_MAX_REQUESTS`，0 = 不回收），仅作内存泄漏兜底。
- 局限：任务存储仍是内存（MemoryJobStore），调度进程**停机期间**错过的时间点不会在启动后补跑
  （超出宽限）。发版尽量避开 15:00–17:30；若错过 15:10 / 17:15，手动执行
  `python manage.py fetch_stock_data --all` / `python manage.py run_detector_scan`。

---

## 4. 低配建议

| 项 | 建议 |
|----|------|
| 自选数量 | 尽量 ≤ 20，盘中任务按股串行 |
| Gunicorn workers | 保持 1；内存仍紧再降 threads |
| 调度进程 | `stocktrace-scheduler` 单独计内存（`MemoryMax=700M`），与 Web 互不拖累 |
| 勿在 VPS 上 `npm run dev` | 只用 `dist` |
| 勿提交 | `db.sqlite3`、`.env`、htpasswd、真实 SECRET_KEY |

---

## 5. HTTPS 回源 + Nginx 限流（安全加固）

旧版为 Flexible SSL：浏览器→Cloudflare 是 HTTPS，但 **CF→源站回源是明文 HTTP**，
Basic Auth 密码裸奔在公网链路上。现改为 **Full (strict)** + 源站证书 + 限流，
配置模板已更新到 `nginx-stocktrace.conf`。已部署的旧版按下面步骤收尾：

### 5.1 签发 Cloudflare Origin 证书（后台操作）

Cloudflare Dashboard → SSL/TLS → Origin Server → Create Certificate，
默认（RSA，15 年有效期，覆盖 `stocktrace.yumeat.cc.cd`）即可。把证书和私钥放到 VPS：

```bash
sudo mkdir -p /etc/ssl/stocktrace
sudo nano /etc/ssl/stocktrace/origin.pem   # 粘贴 Origin Certificate
sudo nano /etc/ssl/stocktrace/origin.key   # 粘贴 Private Key
sudo chmod 600 /etc/ssl/stocktrace/origin.key
```

### 5.2 更新 Nginx 配置

```bash
sudo cp deploy/nginx-stocktrace.conf /etc/nginx/sites-available/stocktrace
sudo nginx -t && sudo systemctl reload nginx
```

新模板做的事：

- **80 端口只做 301 跳转**，业务全走 443（`ssl_protocols TLSv1.2/1.3`）
- **按 Cloudflare 真实 IP 限流**：`set_real_ip_from`（CF 网段，来源
  [cloudflare.com/ips](https://www.cloudflare.com/ips/)，上游更新时需同步）+
  `real_ip_header CF-Connecting-IP`，否则限流会把所有请求算到 CF 节点头上一刀切
- `limit_req_zone $binary_remote_addr rate=10r/s`，`/api/` 与 `/admin/`
  `burst=20 nodelay`，超限返回 429。正常看板 60s 轮询远低于阈值，只挡滥刷
- 进一步可加防火墙：443 只放行 CF 网段、80/8000/8001 全封（服务器侧 ufw/安全组）

### 5.3 切换 Cloudflare SSL 模式（后台操作）

SSL/TLS → Overview → 改为 **Full (strict)**。

验证：

```bash
curl -I https://stocktrace.yumeat.cc.cd/          # 200/401，不再是 526
sudo tail -f /var/log/nginx/access.log            # 日志里的 IP 应是真实客户端 IP
```

注意：Origin 证书只被 Cloudflare 信任，浏览器直连源站 IP 会报证书错误——
这正是预期（源站不该被绕过 CF 直接访问）。

---

## 6. 数据库备份与恢复演练（OPS-01）

`db.sqlite3` 里的市场日度快照、ETF 份额快照、探测器调参流水**只能向前积累、无法重新拉取**，
丢了就永久缺口。库开着 WAL，**禁止用 `cp` 备份**（会丢 `-wal` 里未 checkpoint 的写入，
或拷到撕裂文件）。统一用 `deploy/stocktrace-backup.sh`（`sqlite3 .backup` 在线备份 →
`integrity_check` 校验 → gzip → 保留最近 N 份 → 可选异地）。

### 6.1 安装

```bash
sudo apt install -y sqlite3            # 脚本优先用 CLI；没有时自动退回 python3 标准库
sudo mkdir -p /var/backups/stocktrace
sudo chown www:www /var/backups/stocktrace
sudo chmod 750 /var/backups/stocktrace
sudo chmod +x /opt/stocktrace/deploy/stocktrace-backup.sh

# 可选参数（不建文件则全用默认：每日一份、保留 14 份、不异地）
sudo tee /etc/stocktrace/backup.env >/dev/null <<'ENV'
# STOCKTRACE_DB=/opt/stocktrace/backend/db.sqlite3
# BACKUP_DIR=/var/backups/stocktrace
BACKUP_KEEP=14
# 异地（强烈建议至少每周一份到另一台机器/对象存储）：
# BACKUP_RSYNC_TARGET=backup@nas.example:/srv/stocktrace/
# BACKUP_RSYNC_OPTS=-a -e "ssh -i /home/www/.ssh/backup_ed25519"
# BACKUP_POST_HOOK=rclone copy "$1" crypt-remote:stocktrace/
ENV

sudo cp /opt/stocktrace/deploy/stocktrace-backup.service /opt/stocktrace/deploy/stocktrace-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stocktrace-backup.timer

# 立即手动跑一次并确认
sudo systemctl start stocktrace-backup.service
sudo journalctl -u stocktrace-backup -n 20 --no-pager
ls -lh /var/backups/stocktrace/
systemctl list-timers stocktrace-backup.timer
```

定时：每天 **22:00（Asia/Shanghai）**，此时 15:30–21:00 的收盘后预热/快照与 17:15 探测器扫描都已结束；
`Persistent=true` 保证关机错过时开机补跑。脚本任一步失败都以非 0 退出，unit 会变为 `failed`
（`systemctl --failed` 可见）。异地 rsync / 钩子失败同样视为失败——异地副本是防整机丢失的唯一手段，不能静默。

### 6.2 恢复步骤

```bash
# 1. 停掉所有会写库的进程（Web + 调度）
sudo systemctl stop stocktrace stocktrace-scheduler

# 2. 把现有库挪开（连同 WAL/SHM，别删，以防恢复错了还能回退）
cd /opt/stocktrace/backend
sudo -u www mkdir -p /var/backups/stocktrace/pre-restore
sudo -u www mv db.sqlite3 db.sqlite3-wal db.sqlite3-shm /var/backups/stocktrace/pre-restore/ 2>/dev/null || true

# 3. 解压选定的备份
sudo -u www sh -c 'gunzip -c /var/backups/stocktrace/db-YYYYMMDD-HHMMSS.sqlite3.gz > db.sqlite3'
sudo -u www sqlite3 db.sqlite3 'PRAGMA integrity_check;'      # 必须输出 ok

# 4. 迁移（备份若早于当前代码版本，补齐新迁移）后启动
set -a; source /etc/stocktrace/env; set +a
sudo -u www -E venv/bin/python manage.py migrate
sudo systemctl start stocktrace stocktrace-scheduler
curl -s http://127.0.0.1:8000/api/health/ | python3 -m json.tool
```

### 6.3 每月恢复演练（不碰线上库）

备份没恢复过就不算备份。每月挑最新一份在临时目录里恢复并抽查：

```bash
D=$(mktemp -d)
gunzip -c "$(ls -1 /var/backups/stocktrace/db-*.sqlite3.gz | tail -1)" > "$D/db.sqlite3"
sqlite3 "$D/db.sqlite3" 'PRAGMA integrity_check;'
sqlite3 "$D/db.sqlite3" "SELECT MAX(trade_date), COUNT(*) FROM stocks_marketdailysnapshot;"
sqlite3 "$D/db.sqlite3" "SELECT COUNT(*) FROM stocks_dailyquote;"
rm -rf "$D"
```

期望：`integrity_check` 为 `ok`，最新快照日期是最近一个交易日（或前一交易日），行数与线上同量级。
演练结果记到运维记录里；连续两次失败要排查脚本/磁盘。

---

## 7. 依赖锁定与升级（OPS-02）

- `backend/requirements.txt`：顶层依赖的**允许范围**（关键包有上界：`django>=6.0,<6.2`、
  `apscheduler>=3.10,<4`、`pandas>=2.2,<4`、`akshare==1.18.64` 等）。只在升级时用来重新解析。
- `backend/requirements.lock`：全量精确版本（含传递依赖），**生产只装它**：
  `pip install -r backend/requirements.lock`。
- `backend/requirements-dev.txt`：`-r requirements.txt` + 开发工具（ruff）。

为什么必须锁：APScheduler 4.x 删掉了 `apscheduler.schedulers.background`，一旦漂移过去，调度器
ImportError 不启动；akshare 曾因静默升级导致快照断供；pandas 大版本会改默认 dtype/拷贝语义。

**首次切换到锁文件前**先记录线上实际版本，与锁文件比对（尤其 Django / pandas / numpy）：

```bash
/opt/stocktrace/backend/venv/bin/pip freeze > /tmp/vps-freeze.txt
diff <(sort -f /tmp/vps-freeze.txt) <(grep -v '^#' /opt/stocktrace/backend/requirements.lock | sort -f)
```

有差异时：要么在测试环境装线上版本跑通测试后把锁文件改成线上版本，要么按下面流程升级线上。

**升级流程**（在开发机/测试机，不在生产机上做）：

```bash
cd backend
# 1) 改 requirements.txt 中的范围（例如放开某个上界），然后在全新 venv 里解析
python3.12 -m venv /tmp/lockenv
/tmp/lockenv/bin/pip install -U pip
/tmp/lockenv/bin/pip install -r requirements.txt
# 2) 跑全量测试（断网）+ validate_market_sources（联网只读校验上游字段/单位）
/tmp/lockenv/bin/pip install -r requirements-dev.txt
/tmp/lockenv/bin/python manage.py test stocks
/tmp/lockenv/bin/python manage.py validate_market_sources
# 3) 通过后重新生成锁文件（全新 venv 里只有运行时依赖，freeze 即全集）
{ head -3 requirements.lock; /tmp/lockenv/bin/pip freeze --exclude-editable | grep -viE '^(ruff|pip|setuptools|wheel)=='; } > requirements.lock.new
mv requirements.lock.new requirements.lock
git add requirements.txt requirements.lock && git commit -m "chore(deps): ..."
```

注：第 3 步 freeze 是在装过 requirements-dev 的 venv 里做的，所以要过滤掉开发工具；如果 dev
依赖变多，改为在第 1 步之后（装 dev 之前）先 freeze。akshare 升级必须单独一个提交，并在 VPS 上
用 `validate_market_sources` 实测全集可用后再合入。

---

## 8. 日志与健康检查（REL-02）

### 8.1 日志

`settings.LOGGING` 已配置：所有日志带时间/级别/模块/进程号/线程名输出到 **stdout**，由 systemd 收进 journald：

```bash
sudo journalctl -u stocktrace -f              # Web：请求错误、行情兜底失败（带堆栈）
sudo journalctl -u stocktrace-scheduler -f    # 调度：任务启动/结果、清理条数、job 失败/错过
sudo journalctl -u stocktrace-scheduler --since today | grep -E "ERROR|WARNING"
```

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `STOCKTRACE_LOGLEVEL` | `INFO` | `stocks.*` 业务日志级别（排障时可设 `DEBUG`） |
| `STOCKTRACE_LOG_FILE` | 空 | 另写一份文件（`WatchedFileHandler`，配合 logrotate）。Web 与调度是两个进程，建议分开：在各自 unit 里用 `Environment=STOCKTRACE_LOG_FILE=/var/log/stocktrace/web.log` / `scheduler.log`，并 `mkdir -p /var/log/stocktrace && chown www:www` |

`apscheduler` 只输出 WARNING 以上（job 抛异常、错过执行时间都会出现）。

### 8.2 健康检查 `GET /api/health/`

返回 DB 可用性、调度器心跳（调度进程每 60s 写 `/run/stocktrace/scheduler_heartbeat.json`）、
各 job 的 `next_run_time`、各 job 最近一次结果（`last_runs`：ok / error / missed + 时间）、最近一次市场快照日期。

- 200 `{"status": "ok"}`：DB 可用，且调度心跳在 `STOCKTRACE_HEARTBEAT_STALE_SECONDS`（默认 180s）内；
- 503 `{"status": "degraded"}`：DB 不可用，或调度进程没在跑 / 心跳过期（`STOCKTRACE_SCHEDULER=0` 时不计调度）。
- Nginx 只放行 127.0.0.1；本机直接打 Gunicorn：

```bash
curl -s http://127.0.0.1:8000/api/health/ | python3 -m json.tool
```

### 8.3 本机探测与告警（建议）

```bash
# /etc/cron.d/stocktrace-health   每 5 分钟（root：需读取 /etc/stocktrace/env）；失败写 syslog，配置了 STOCKTRACE_PUSH_URL 时顺带推送
*/5 * * * * root curl -fsS -m 10 http://127.0.0.1:8000/api/health/ >/dev/null || { logger -t stocktrace "health degraded"; . /etc/stocktrace/env; [ -n "$STOCKTRACE_PUSH_URL" ] && curl -fsS -m 10 -d "title=StockTrace 健康检查失败" -d "desp=$(curl -s -m 10 http://127.0.0.1:8000/api/health/ | head -c 1500)" "$STOCKTRACE_PUSH_URL" >/dev/null; }
```

也可以用 uptime-kuma 等工具探测 `http://127.0.0.1:8000/api/health/`（关键字 `"status": "ok"`）。
