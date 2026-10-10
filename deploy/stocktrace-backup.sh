#!/usr/bin/env bash
# StockTrace SQLite 在线备份（OPS-01）
#
# 为什么用 .backup 而不是 cp：库开了 WAL，cp 只拷主文件会丢掉尚未 checkpoint
# 的 -wal 内容，且拷贝过程中有写入会得到撕裂的文件；sqlite3 .backup 走在线
# 备份 API，服务不停也能拿到一致快照。
#
# 流程：.backup → PRAGMA integrity_check 校验 → gzip → 按份数轮转 → 可选异地钩子。
# 任一步失败以非 0 退出（systemd 会把 unit 标成 failed，journalctl 可查）。
#
# 环境变量（可写在 /etc/stocktrace/backup.env，由 stocktrace-backup.service 加载）：
#   STOCKTRACE_DB          源库路径        默认 /opt/stocktrace/backend/db.sqlite3
#   BACKUP_DIR             备份目录        默认 /var/backups/stocktrace
#   BACKUP_KEEP            保留份数        默认 14（每日一份 ≈ 两周）
#   BACKUP_RSYNC_TARGET    可选，rsync 异地目标（如 backup@nas:/srv/stocktrace/）
#   BACKUP_RSYNC_OPTS      可选，rsync 额外参数（默认 -a）
#   BACKUP_POST_HOOK       可选，备份成功后执行的命令，备份文件路径作为 $1
#                          （如 rclone copy 到对象存储；自行负责加密）
#
# 用法：
#   sudo -u www /opt/stocktrace/deploy/stocktrace-backup.sh
set -euo pipefail

DB="${STOCKTRACE_DB:-/opt/stocktrace/backend/db.sqlite3}"
DIR="${BACKUP_DIR:-/var/backups/stocktrace}"
KEEP="${BACKUP_KEEP:-14}"

log() { echo "[stocktrace-backup] $(date '+%F %T') $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

[[ "$KEEP" =~ ^[0-9]+$ ]] && (( KEEP >= 1 )) || die "BACKUP_KEEP 必须是 >=1 的整数：$KEEP"
[[ -f "$DB" ]] || die "源库不存在：$DB"
mkdir -p "$DIR"

STAMP="$(date '+%Y%m%d-%H%M%S')"
TMP="$DIR/.db-$STAMP.sqlite3.partial"
OUT="$DIR/db-$STAMP.sqlite3"
trap 'rm -f "$TMP"' EXIT

# 1) 在线一致性备份（优先 sqlite3 CLI；没装 CLI 时退回 Python 标准库同一套 API）
if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB" ".timeout 20000" ".backup '$TMP'"
else
    python3 - "$DB" "$TMP" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1], timeout=20)
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
PY
fi

# 2) 校验：备份文件必须能通过 integrity_check，否则这份备份不可信
if command -v sqlite3 >/dev/null 2>&1; then
    CHECK="$(sqlite3 "$TMP" 'PRAGMA integrity_check;')"
else
    CHECK="$(python3 -c 'import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute("PRAGMA integrity_check").fetchone()[0])' "$TMP")"
fi
[[ "$CHECK" == "ok" ]] || die "integrity_check 未通过：$CHECK"

# 3) 压缩落盘（先写 .partial 再改名，轮转永远不会删到半成品）
mv "$TMP" "$OUT"
gzip -f "$OUT"
OUT="$OUT.gz"
log "备份完成：$OUT ($(du -h "$OUT" | cut -f1))"

# 4) 按份数轮转：文件名带时间戳，字典序即时间序
mapfile -t ALL < <(ls -1 "$DIR"/db-*.sqlite3.gz 2>/dev/null | sort)
if (( ${#ALL[@]} > KEEP )); then
    for f in "${ALL[@]:0:${#ALL[@]}-KEEP}"; do
        rm -f -- "$f"
        log "轮转删除：$f"
    done
fi

# 5) 可选异地：失败视为备份失败（异地是防"整机丢失"的唯一手段，不能静默）
if [[ -n "${BACKUP_RSYNC_TARGET:-}" ]]; then
    # shellcheck disable=SC2086
    rsync ${BACKUP_RSYNC_OPTS:--a} "$OUT" "$BACKUP_RSYNC_TARGET" || die "rsync 异地同步失败：$BACKUP_RSYNC_TARGET"
    log "已同步到 $BACKUP_RSYNC_TARGET"
fi
if [[ -n "${BACKUP_POST_HOOK:-}" ]]; then
    bash -c "$BACKUP_POST_HOOK" _ "$OUT" || die "BACKUP_POST_HOOK 执行失败"
    log "BACKUP_POST_HOOK 已执行"
fi
