# StockTrace 部署说明

目标环境：Ubuntu 22.04，**1H2G** VPS，路径 `/opt/stocktrace`，Nginx + Gunicorn + systemd，Cloudflare Full (strict) SSL（回源加密 + Nginx 限流，见第 5 节），HTTP Basic Auth。

VPS 上的 `/opt/stocktrace` 是本仓库的 git 工作副本，按发布标签（`vX.Y.Z`）部署，升级与回滚见第 2 节。

---

## 目录

| 节 | 内容 |
|---|---|
| 1 | 首次部署（Ubuntu 22.04） |
| 2 | 升级与回滚 |
| 3 | 运维命令与调度器 |
| 4 | 低配建议 |
| 5 | HTTPS 回源、限流、防火墙、systemd 沙箱 |
| 6 | 数据库备份与恢复演练 |
| 7 | 依赖锁定与升级 |
| 8 | 日志与健康检查 |
| 9 | 密钥管理 |

历次变更见 `git log` 与发布标签说明，本文只描述当前版本的部署方式（OPS-09：原「相对 v1.01 的变更」一节已过时，已删除）。

---

## 1. 首次部署（Ubuntu 22.04，OPS-07）

运行时版本（与 CI 一致）：**Python 3.12**（`.python-version`）、**Node.js 22 LTS ≥ 22.13**
（`frontend/.nvmrc`、`package.json` 的 `engines`）。Ubuntu 22.04 自带 Python 3.10、Node 12，
都不能用，按下面装。以下命令以有 sudo 权限的管理员执行，从上到下顺序不能乱
（nginx 配置引用了证书，证书不在时 `nginx -t` 会失败）。

```bash
# 1) 系统依赖：Python 3.12 来自 deadsnakes PPA，Node 22 来自 NodeSource
sudo apt update
sudo apt install -y software-properties-common ca-certificates curl git \
    nginx apache2-utils sqlite3 ufw
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt install -y python3.12 python3.12-venv python3.12-dev build-essential
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
python3.12 --version    # 3.12.x
node --version          # v22.13 及以上

# 2) 服务账号 www（系统用户、不能登录；HOME 由 systemd 单元指到 /var/cache/stocktrace）
id www 2>/dev/null || sudo useradd --system --user-group --no-create-home \
    --home-dir /nonexistent --shell /usr/sbin/nologin www

# 3) 代码：克隆到 /opt/stocktrace 并切到要发布的标签。
# 属主（SEC-10）：代码归 root，www 只拥有 backend/ 目录本身（SQLite 库与 WAL/-shm 需要在此建文件）
# 和 staticfiles/；不要 chown -R，否则 Web 进程被攻破时可以改写全部代码与前端产物
sudo git clone <仓库地址> /opt/stocktrace
sudo git -C /opt/stocktrace checkout --detach vX.Y.Z
sudo chown www:www /opt/stocktrace/backend
sudo install -d -o www -g www -m 0755 /opt/stocktrace/staticfiles
cd /opt/stocktrace

# 4) 虚拟环境（backend/venv，归 root；装锁文件，全量精确版本，OPS-02）
sudo python3.12 -m venv backend/venv
sudo backend/venv/bin/pip install --upgrade pip
sudo backend/venv/bin/pip install -r backend/requirements.lock

# 5) 环境变量：含 SECRET_KEY / Fernet 密钥，只允许 root 读（systemd 以 root 读取 EnvironmentFile，SEC-10）
sudo install -d -m 755 /etc/stocktrace
sudo install -m 600 -o root -g root deploy/env.example /etc/stocktrace/env
python3 -c "import secrets; print(secrets.token_urlsafe(64))"          # → DJANGO_SECRET_KEY
backend/venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # → STOCKTRACE_FERNET_KEY
sudo nano /etc/stocktrace/env   # 填上面两个密钥、DJANGO_ALLOWED_HOSTS、STOCKTRACE_TRUSTED_ORIGINS（你的域名）
# 可选：STOCKTRACE_PUSH_URL（Server酱兼容；价格提醒 + 快照自检告警推送，
# 未配置则静默跳过）、MARKET_SNAPSHOT_RETENTION_DAYS（快照保留天数，默认 600，下限 400）、
# STOCKTRACE_STATIC_ROOT（collectstatic 目录，默认 /opt/stocktrace/staticfiles）

# 6) 数据库与静态文件：manage.py 一律以 www 身份运行（root 建出的库/WAL 会让服务报只读）。
# 环境变量只对 systemd 进程自动生效，手动跑 manage.py 先在 root shell 里加载，再 -E 传给 www
sudo -i
cd /opt/stocktrace/backend
set -a; source /etc/stocktrace/env; set +a
sudo -u www -E venv/bin/python manage.py check --deploy
sudo -u www -E venv/bin/python manage.py migrate
sudo -u www -E venv/bin/python manage.py collectstatic --noinput
exit

# 7) 前端构建（root 构建，dist 归 root，nginx 只读）
cd /opt/stocktrace/frontend
sudo npm ci
sudo npm run build

# 8) systemd：Web（gunicorn）与调度器（run_scheduler）是两个独立单元（REL-01）
sudo cp /opt/stocktrace/deploy/stocktrace.service /opt/stocktrace/deploy/stocktrace-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stocktrace stocktrace-scheduler
curl -s http://127.0.0.1:8000/api/health/ | python3 -m json.tool   # status ok、scheduler.alive true

# 9) 源站证书（第 5.1 节：Cloudflare 后台签发 Origin 证书，放到 /etc/ssl/stocktrace/）
#    完成后再继续下一步

# 10) Nginx + Basic Auth：<username> 换成不显眼的用户名（不要用 stocktrace/admin 等），
# 密码用长随机串（如 openssl rand -base64 24），用户名与密码都不要写进仓库（SEC-02）
sudo htpasswd -B -c /etc/nginx/.htpasswd_stocktrace <username>
sudo sed "s/stocktrace.yumeat.cc.cd/<你的域名>/g" /opt/stocktrace/deploy/nginx-stocktrace.conf \
    | sudo tee /etc/nginx/sites-available/stocktrace >/dev/null
sudo ln -sf /etc/nginx/sites-available/stocktrace /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# 11) 防火墙：80/443 只放行 Cloudflare 网段，防止绕过 CF 直连源站（SEC-10，见 5.2）
sudo bash /opt/stocktrace/deploy/ufw-cloudflare.sh --dry-run   # 先看清要执行的规则
sudo bash /opt/stocktrace/deploy/ufw-cloudflare.sh

# 12) Cloudflare SSL 模式切到 Full (strict)（第 5.3 节），然后装备份定时器（第 6.1 节）
```

注意：

- **Gunicorn 配置不要设 `user=`**，systemd 已是 `User=www`；配置只有 `backend/gunicorn_config.py` 一份（REL-10）
- Cloudflare SSL 模式：**Full (strict)**，需先配源站证书，见第 5 节
- 域名：nginx 的 `server_name`、`DJANGO_ALLOWED_HOSTS`、`STOCKTRACE_TRUSTED_ORIGINS` 三处要一致
- 第一次部署后自选为空，在页面添加股票即自动拉行情；探测器的融资余额等快照要积累约一周

---

## 2. 升级与回滚（OPS-05）

原则：

- **只部署打过标签、CI 已通过的版本**（`git tag -a vX.Y.Z`，见 README 发版流程），生产目录是该仓库的
  git 工作副本，用 `git checkout <tag>` 切版本，**不在生产机上改代码、不在生产目录 `git init`/`push`**。
- **测试不在生产机上跑**：`manage.py test` 会触网（akshare 上游）且依赖开发依赖，在生产机停服后跑
  既拉长停机、又可能"必红"。测试只在 CI，或非生产机断网执行（见 7 节、仓库 CI 配置）。
- 生产上只做**只读检查**：`check --deploy`、`migrate --plan`、启动后 `curl /api/health/`。
- 每次升级前**先记录当前 tag、再做一次在线备份**，回滚才有据可依。

以下命令以 root 执行：git / pip / npm 以 root 运行（代码归 root，SEC-10）；**manage.py 一律以 `www`
身份运行**（它会写库，root 建出的 -wal/-shm 会导致「attempt to write a readonly database」）。
`NEW=vX.Y.Z` 换成要发布的标签。

### 2.1 升级

```bash
NEW=vX.Y.Z
cd /opt/stocktrace
G="git -C /opt/stocktrace"

# 1) 记录当前版本（回滚目标），工作区必须干净（有改动说明有人在生产上手改过，先查清再继续）
$G status --porcelain --untracked-files=no      # 应无输出
PREV=$($G describe --tags --exact-match 2>/dev/null || $G rev-parse HEAD)
echo "$(date -Is) $PREV -> $NEW" | sudo tee -a /var/backups/stocktrace/deploy-history.log

# 2) 升级前在线一致性备份（第 6 节；WAL 模式禁止 cp）
sudo -u www /opt/stocktrace/deploy/stocktrace-backup.sh

# 3) 切到新标签（只 fetch，不 push）
$G fetch --tags origin
$G checkout --detach "$NEW"

# 4) 依赖：一律装锁文件，不要装 requirements.txt、不要 pip install -U（第 7 节）
backend/venv/bin/pip install -r backend/requirements.lock

# 5) 只读检查（服务仍在旧版本上运行）
cd /opt/stocktrace/backend
set -a; source /etc/stocktrace/env; set +a       # 手动 manage.py 必须加载环境变量
sudo -u www -E venv/bin/python manage.py check --deploy
sudo -u www -E venv/bin/python manage.py migrate --plan   # 看清本次要执行哪些迁移

# 6) 前端构建（不影响运行中的服务；dist 原地替换）
cd /opt/stocktrace/frontend
npm ci && npm run build

# 7) 停服 → 迁移 → 静态文件 → 同步 systemd 单元 → 启动
sudo systemctl stop stocktrace stocktrace-scheduler
cd /opt/stocktrace/backend
sudo -u www -E venv/bin/python manage.py migrate
sudo -u www -E venv/bin/python manage.py collectstatic --noinput
sudo cp /opt/stocktrace/deploy/stocktrace.service /opt/stocktrace/deploy/stocktrace-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable stocktrace stocktrace-scheduler
sudo systemctl start stocktrace stocktrace-scheduler

# 8) 验证
curl -s http://127.0.0.1:8000/api/health/ | python3 -m json.tool
sudo journalctl -u stocktrace-scheduler -n 50 --no-pager | grep -i APScheduler
sudo journalctl -u stocktrace -n 200 --no-pager | grep -i APScheduler    # 应无输出
```

期望：

- `check --deploy` 只剩 `security.W004`（HSTS 默认关闭，有意为之，见 `STOCKTRACE_HSTS_SECONDS`）与
  `security.W008`（HTTP→HTTPS 跳转由 Cloudflare/Nginx 完成）；出现其他告警（尤其 W009 SECRET_KEY）先修再继续。
- `/api/health/` 返回 `"status": "ok"`、`scheduler.alive: true`、各 job 的 `next_run_time`；
  `stocktrace-scheduler` 日志里 `APScheduler 已启动 ...，独立进程 pid=NNN` 出现一次。
- 发版尽量避开 15:00–17:30（收盘汇总与 17:15 探测器）。
- `/etc/stocktrace/env` 里若还有 `STOCKTRACE_RUN_SCHEDULER_IN_WEB=1` 要删掉，否则 Web 会继续在进程内跑调度。

### 2.2 回滚

`deploy-history.log` 最后一行是本次的 `PREV -> NEW`。

```bash
PREV=v上一个标签            # 取自 /var/backups/stocktrace/deploy-history.log
cd /opt/stocktrace
G="git -C /opt/stocktrace"
sudo systemctl stop stocktrace stocktrace-scheduler
```

- **本次 `migrate --plan` 没有新迁移**：直接切回旧代码即可。

  ```bash
  $G checkout --detach "$PREV"
  backend/venv/bin/pip install -r backend/requirements.lock
  (cd frontend && npm ci && npm run build)
  sudo cp deploy/stocktrace.service deploy/stocktrace-scheduler.service /etc/systemd/system/ && sudo systemctl daemon-reload
  sudo systemctl start stocktrace stocktrace-scheduler
  curl -s http://127.0.0.1:8000/api/health/ | python3 -m json.tool
  ```
- **有新迁移**：旧代码不认识新表结构，必须先把库退回去，二选一：
  1. **恢复升级前的备份**（最稳妥；会丢失升级后写入的数据，通常只有几分钟）：按第 6.2 节恢复
     第 2 步生成的那份备份，**跳过 6.2 里的 migrate**，再执行上面的「切回旧代码」。
  2. **反向迁移**（保留新数据；仅当新迁移都可逆时）：在**新代码**下先
     `sudo -u www -E venv/bin/python manage.py migrate stocks <旧版本最后一个迁移号>`，再切回旧代码。
     不确定可逆性就用方案 1。
- 回滚后在 `deploy-history.log` 追加一行 `ROLLBACK NEW -> PREV` 和原因。

---

## 3. 运维命令

```bash
sudo systemctl status stocktrace
sudo systemctl restart stocktrace
sudo journalctl -u stocktrace -f

# 手动拉数（root shell 里先加载环境变量，再以 www 运行；有失败时退出码非 0，REL-09）
cd /opt/stocktrace/backend
set -a; source /etc/stocktrace/env; set +a
sudo -u www -E venv/bin/python manage.py fetch_stock_data --all            # 轻量：分钟线近 2 个交易日
sudo -u www -E venv/bin/python manage.py fetch_stock_data --all --days 5   # 分钟线回溯 5 个交易日（上限 5）

# 只读校验 ETF/板块上游字段与单位；不写数据库、不修改缓存
sudo -u www -E venv/bin/python manage.py validate_market_sources

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
  （盘中 5 分钟任务 240s，08:50 预热 1800s，15:10 汇总 3600s，探测器 1500s，收盘后预热 900s）。
  宽限内晚到的任务仍会执行（各任务自带交易日 / 收盘时间守卫）。
- 任务链逐步容错（REL-08）：15:10 汇总里任一步（日线、提醒、AI 流水清理、快照清理）异常只记带堆栈的
  日志，不影响后续步骤。探测器 17:15 先检查当日 `industry_ff`/`market_ff` 快照，缺失则在
  17:45/18:15/18:45 重试，最后一次降级照跑；当日已有成功扫描记录时重试点直接跳过。
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
| 勿提交 | `db.sqlite3`、`.env`、htpasswd（含用户名）、真实 SECRET_KEY |

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
- `limit_req_zone $binary_remote_addr rate=10r/s`，**server 级** `burst=20 nodelay`
  （`/`、`/static/` 也计入，防止借静态页高速试 Basic Auth 密码；SEC-02），`/admin/` 另叠加
  `zone=admin`（30r/m，burst=10），超限返回 429。正常看板 60s 轮询远低于阈值，只挡滥刷
- **防火墙只放行 CF 网段**（SEC-10）：`sudo bash deploy/ufw-cloudflare.sh`（先 `--dry-run`）。
  它拉取 cloudflare.com/ips 的最新网段，80/443 只对这些网段开放，并保留 SSH。不能用 nginx 的
  allow/deny 代替：real_ip 已把 `$remote_addr` 换成 `CF-Connecting-IP`，直连源站时这个头可以伪造。
  云厂商安全组若也能配，同样只放行 CF 网段。
- **安全响应头**（SEC-10）：`server_tokens off`、`X-Content-Type-Options: nosniff`、
  `X-Frame-Options: DENY`、`Referrer-Policy: same-origin`；`/assets/`（带哈希的构建产物）一年
  `immutable` 缓存，`index.html` `no-cache`，发版后不会因旧入口页引用已删除的资源而白屏。
  验证：`curl -sI -u <user>:<pass> https://<域名>/ | grep -iE 'server|x-frame|nosniff|referrer|cache-control'`

### 5.3 切换 Cloudflare SSL 模式（后台操作）

SSL/TLS → Overview → 改为 **Full (strict)**。

验证：

```bash
curl -I https://stocktrace.yumeat.cc.cd/          # 200/401，不再是 526
sudo tail -f /var/log/nginx/access.log            # 日志里的 IP 应是真实客户端 IP
```

注意：Origin 证书只被 Cloudflare 信任，浏览器直连源站 IP 会报证书错误——
这正是预期（源站不该被绕过 CF 直接访问）。

### 5.4 Basic Auth 防爆破与应用层认证（SEC-02）

应用本身没有认证（DRF `AllowAny`），**唯一的门是 Nginx Basic Auth**，所以：

1. **用户名 + 长随机密码**：见第 1 节，文档与仓库里一律写 `<username>`。已部署的旧用户名若曾出现在
   公开文档里，换一个：`sudo htpasswd -B /etc/nginx/.htpasswd_stocktrace <新用户名>`，
   再 `sudo htpasswd -D /etc/nginx/.htpasswd_stocktrace <旧用户名>`，`reload nginx`。
2. **限流**：已在 server 级生效（5.2）。
3. **fail2ban**：`deploy/fail2ban-stocktrace.local`（`nginx-http-auth` jail，10 分钟失败 5 次封 1 小时）。
   ⚠️ 站点在 Cloudflare 后面，**默认的 iptables 动作无效**（连接来自 CF 节点）；文件里给了两种动作：
   写 nginx deny 文件（`/etc/nginx/stocktrace-deny.d/`，nginx 配置已 include）或调 Cloudflare API。

   ```bash
   sudo apt install -y fail2ban
   sudo cp deploy/fail2ban-stocktrace.local /etc/fail2ban/jail.d/stocktrace.local
   sudo nano /etc/fail2ban/action.d/stocktrace-nginx-deny.conf   # 内容见 .local 文件注释
   sudo fail2ban-client reload
   sudo fail2ban-client status nginx-http-auth
   # 验证：另一台机器连续输错 5 次密码 → 之后返回 403；解封：
   sudo fail2ban-client set nginx-http-auth unbanip <ip>
   ```
4. **中期：应用层认证**（未实现，仅记录方案）。任选其一，Basic Auth 退为第二道防线：
   - Cloudflare Zero Trust **Access**（零代码，邮箱 OTP / SSO 登录后才放行到源站）；
   - DRF 加 `SessionAuthentication` + `IsAuthenticated` + 登录页（需同时把 /api/ 纳入 Django CSRF，
     并与 `StockTrace/middleware.py` 的来源校验协调）。

### 5.5 systemd 沙箱与目录属主（SEC-10）

`stocktrace.service` / `stocktrace-scheduler.service` 已开启 `NoNewPrivileges`、`ProtectSystem=strict`、
`ProtectHome`、`PrivateTmp` 等沙箱选项：整个文件系统只读，只有以下位置可写——

| 路径 | 来源 | 用途 |
|---|---|---|
| `/opt/stocktrace/backend` | `ReadWritePaths=` | SQLite 库与 `-wal`/`-shm`、`__pycache__` |
| `/run/stocktrace` | `RuntimeDirectory=` | 调度锁、任务锁、心跳 |
| `/var/log/stocktrace` | `LogsDirectory=` | 可选的 `STOCKTRACE_LOG_FILE` |
| `/var/cache/stocktrace` | `CacheDirectory=` | `HOME`/`XDG_CACHE_HOME`，第三方库的缓存 |

内存：`MemoryHigh` 是软上限（超过后内核积极回收，进程变慢但不被杀），`MemoryMax` 是硬上限（超过即 OOM
kill，再由 `Restart=always` 拉起）。两个单元都 `After=network-online.target`。

已部署实例（旧文档让 www 拥有整个 `/opt/stocktrace`）一次性改属主：

```bash
sudo systemctl stop stocktrace stocktrace-scheduler
sudo chown -R root:root /opt/stocktrace
sudo chown www:www /opt/stocktrace/backend
sudo sh -c 'chown www:www /opt/stocktrace/backend/db.sqlite3*'
sudo chown -R www:www /opt/stocktrace/staticfiles
sudo chmod 600 /etc/stocktrace/env && sudo chown root:root /etc/stocktrace/env
sudo cp /opt/stocktrace/deploy/stocktrace*.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl start stocktrace stocktrace-scheduler
sudo journalctl -u stocktrace -u stocktrace-scheduler -n 100 --no-pager | grep -iE 'denied|read-only|error'
```

出现 `Read-only file system` / `Permission denied` 时，说明有代码写到了上表以外的位置：先记下路径，
临时在对应单元加 `ReadWritePaths=<路径>` 恢复服务，再反馈修正代码或单元文件。

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
| `STOCKTRACE_LOG_FILE` | 空 | 另写一份文件（`WatchedFileHandler`，配合 logrotate）。Web 与调度是两个进程，建议分开：在各自 unit 里用 `Environment=STOCKTRACE_LOG_FILE=/var/log/stocktrace/web.log` / `scheduler.log`。两个 unit 已声明 `LogsDirectory=stocktrace`，systemd 自动创建属主为 www 的 `/var/log/stocktrace`（沙箱下只有它可写） |

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

---

## 9. 密钥管理（SEC-04）

- `DJANGO_SECRET_KEY`：生产（`DJANGO_DEBUG=False`）拒绝 env.example 占位符和短于 50 字符的值，
  启动直接报 `ImproperlyConfigured`。生成：`python3 -c "import secrets; print(secrets.token_urlsafe(64))"`。
- `STOCKTRACE_FERNET_KEY`：AI 服务商 API Key 的加密密钥，与 SECRET_KEY 解耦。逗号分隔多个，
  **第一个用于加密，其余只用于解密**。未配置时回退为由 SECRET_KEY 派生的旧密钥（日志有 WARNING）。
  旧派生密钥始终参与解密，所以配置新密钥不会让旧密文失效。

### 9.1 首次切换到独立加密密钥

```bash
cd /opt/stocktrace/backend
sudo -u www /opt/stocktrace/deploy/stocktrace-backup.sh          # 先备份
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
sudo nano /etc/stocktrace/env      # STOCKTRACE_FERNET_KEY=<上面生成的值>；SECRET_KEY 先不要动
set -a; source /etc/stocktrace/env; set +a
venv/bin/python manage.py rotate_api_keys --dry-run   # 确认全部能解开
sudo -u www -E venv/bin/python manage.py rotate_api_keys
sudo systemctl restart stocktrace stocktrace-scheduler
```

完成后已存密文只依赖 `STOCKTRACE_FERNET_KEY`，此后可以独立轮换 `DJANGO_SECRET_KEY`
（轮换 SECRET_KEY 只会让现有会话/签名失效）。**务必把 `STOCKTRACE_FERNET_KEY` 另存一份到密码管理器**：
丢了它，数据库备份里的 API Key 就无法还原，只能在设置页重新录入。

### 9.2 轮换加密密钥

```bash
# 1) 新 key 放在最前，旧 key 保留在后面：STOCKTRACE_FERNET_KEY=<新>,<旧>
sudo nano /etc/stocktrace/env
set -a; source /etc/stocktrace/env; set +a
sudo -u www -E venv/bin/python manage.py rotate_api_keys
sudo systemctl restart stocktrace stocktrace-scheduler
# 2) 确认设置页 AI 服务商能正常测试后，从 env 删掉旧 key，再重启一次
```

命令只在内存中解密、不输出明文；任一条解不开会整体回滚并列出服务商 id，可在设置页重新录入后重跑。
