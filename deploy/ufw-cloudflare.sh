#!/usr/bin/env bash
# 源站 80/443 只放行 Cloudflare 网段（SEC-10）
#
# 为什么在防火墙层做：nginx 用 real_ip 把 $remote_addr 换成了 CF-Connecting-IP，
# 在 nginx 里 allow/deny 判断的是"声称的"客户端 IP，挡不住有人直连源站 IP 并伪造该头。
# 只放行 CF 网段后，绕过 Cloudflare 直连源站的请求在 TCP 层就被丢弃。
#
# 用法（root）：
#   sudo bash /opt/stocktrace/deploy/ufw-cloudflare.sh          # 拉取最新 CF 网段并应用
#   sudo bash /opt/stocktrace/deploy/ufw-cloudflare.sh --dry-run  # 只打印将执行的命令
#
# 注意：
# - 先确保 SSH 规则存在（脚本会 ufw allow OpenSSH），否则启用 ufw 可能把自己锁在外面；
# - CF 网段偶有变动（https://www.cloudflare.com/ips/），变动时重跑本脚本，并同步
#   nginx-stocktrace.conf 里的 set_real_ip_from；
# - 8000（gunicorn）只监听 127.0.0.1，本就不对外。
set -euo pipefail

DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1
run() { if [[ $DRY -eq 1 ]]; then echo "+ $*"; else "$@"; fi; }

fetch() {
  curl -fsS --max-time 15 "$1" || { echo "拉取 $1 失败，未做任何改动" >&2; exit 1; }
}

V4=$(fetch https://www.cloudflare.com/ips-v4)
V6=$(fetch https://www.cloudflare.com/ips-v6)
[[ -n "$V4" && -n "$V6" ]] || { echo "CF 网段为空，未做任何改动" >&2; exit 1; }

command -v ufw >/dev/null || { echo "未安装 ufw：sudo apt install -y ufw" >&2; exit 1; }

run ufw allow OpenSSH
run ufw default deny incoming
run ufw default allow outgoing

# 删掉旧的"任何来源放行 80/443"规则（不存在时忽略）
for port in 80 443; do
  run ufw delete allow "$port/tcp" 2>/dev/null || true
  run ufw delete allow "$port" 2>/dev/null || true
done
run ufw delete allow 'Nginx Full' 2>/dev/null || true
run ufw delete allow 'Nginx HTTPS' 2>/dev/null || true

for net in $V4 $V6; do
  run ufw allow proto tcp from "$net" to any port 80,443 comment 'cloudflare'
done

run ufw --force enable
run ufw status numbered
