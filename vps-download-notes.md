# VPS 文件下载到本地

> ⚠️ **安全警示（SEC-06）**：本文旧版的「方案 A：在生产目录 `git init && git add -A && git push --force`」
> 与「方案 C：`python3 -m http.server --bind 0.0.0.0` 临时下载」已删除，**不要再这样做**：
>
> - 生产目录里有 `db.sqlite3`（含加密的 AI Key 与不可再生的快照）、`-wal/-shm`、备份文件和日志，
>   `git add -A` 很容易把它们推到公开仓库；`--force` 还会覆盖远端历史。
>   代码本来就在仓库里，生产目录只是某个发布标签的 checkout（见 `deploy/DEPLOY.md` 第 2 节），无需从 VPS 回传。
> - `http.server` 无认证、监听公网，等于把目录裸奔到互联网。
>
> 数据库备份用 `deploy/stocktrace-backup.sh`（DEPLOY.md 第 6 节）生成，再用下面的 scp/rsync 拉回本地。

## 核心原则

- 源码：直接从 git 仓库拉取，**不从 VPS 回传**
- 数据库 / 一次性备份：先用备份脚本生成，再走 SSH（scp/rsync）下载
- 跨洋拉大文件慢时，优先给 SSH 配代理（如 `ProxyCommand`），而不是临时开放公网端口

---

## 方案 B：打包后通过 Nginx 域名下载（当前配置下不可用）

> 现行 `deploy/nginx-stocktrace.conf` 对 `*.tar|*.gz|*.zip` 一律 `deny all`，此方案默认会 403。
> 如确需使用，请临时用随机文件名、下载后**立即删除**，且绝不要打包数据库或 `/etc/stocktrace`。

适合：整目录备份、压缩包较大、不想开额外端口。

```bash
# 在 VPS 上打包（排除环境与缓存）
cd /opt/stocktrace
tar czf /opt/stocktrace/frontend/dist/stocktrace-backup.tar.gz \
  --exclude='backend/venv' \
  --exclude='frontend/node_modules' \
  --exclude='frontend/dist' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='db.sqlite3' \
  backend/ frontend/
```

本地浏览器访问（会走 VPN，并经过 Nginx Basic Auth）：

```text
https://stocktrace.yumeat.cc.cd/stocktrace-backup.tar.gz
```

下载完成后清理：

```bash
# 在 VPS 上
rm -f /opt/stocktrace/frontend/dist/stocktrace-backup.tar.gz
```

---

## 方案 D：scp / rsync（直连，速度常慢）

适合：同机房、内网、或 SSH 本身很快时。

```bash
# 先在 VPS 打包
ssh root@<VPS_IP> "cd /opt/stocktrace && tar czf /tmp/stocktrace-backup.tar.gz \
  --exclude='backend/venv' \
  --exclude='frontend/node_modules' \
  --exclude='frontend/dist' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='db.sqlite3' \
  backend/ frontend/"

# 本地下载（-C 开启传输压缩）
scp -C root@<VPS_IP>:/tmp/stocktrace-backup.tar.gz ./
```

说明：
- 很多机场只代理浏览器流量，不代理 SSH，所以 scp 可能很慢
- 中断下载：终端 `Ctrl + C`

---

## 打包时建议排除

```text
backend/venv/
frontend/node_modules/
frontend/dist/
**/__pycache__/
**/*.pyc
db.sqlite3
.env
*.log
```

---

## 本地下载后解压

```bash
# Linux / macOS / Git Bash
mkdir -p vps-backup
tar xzf stocktrace-backup.tar.gz -C vps-backup
```

```powershell
# Windows PowerShell（若已装 tar）
mkdir vps-backup
tar -xzf stocktrace-backup.tar.gz -C vps-backup
```

---

## 快速决策

| 场景 | 推荐 |
|------|------|
| 源码 | 直接 `git clone`/`git pull` 仓库（不从 VPS 回传） |
| 数据库备份 | `stocktrace-backup.sh` 生成后 scp/rsync |
| 整包备份，文件较大 | scp/rsync（必要时给 SSH 配代理） |

---

## 安全提醒

1. 不要把密钥、`.htpasswd`、数据库明文长期放公网可访问路径
2. 不要为下载临时开放公网端口（无认证的 `http.server` 等）
3. 通过 Nginx 下载时，下载完尽快删除 `dist` 下的 tar 包
4. 不要在生产目录 `git init` / `git add -A` / `git push`（更不要 `--force`）
