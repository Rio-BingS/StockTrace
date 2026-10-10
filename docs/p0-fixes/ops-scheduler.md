# P0 修复说明：运维与调度（分支 fix/p0-ops-scheduler）

测试：`stv/bin/python nonet_run.py stocks` → 237 个测试（基线 214 + 新增 23，见 `backend/stocks/tests_p0_ops.py`），
1 个失败 + 2 个错误，**全是基线已有、与本分支无关**：
- `tests_features.test_sina_fallback_when_em_fails`：已知的写死日期失败。
- `tests_detector.DegradedScanTests.test_all_sources_down_produces_no_fabricated_signals`、
  `tests_detector.SectorSignalScanTests.test_calm_sector_with_two_aligned_dims_fires_signal`：依赖墙钟时间，
  在 15:30 之前跑时，探测器收盘守卫返回 skipped（没有 `status` 键）。在未改动的基线提交 cc558eb 上同一时刻
  （2026-10-09 00:20 CST）跑也一样失败（COMMON.md 记录的基线很可能是在 15:30 之后跑出来的）。交给 detector 方向处理（这些测试应 mock 时间）。

`ruff check .`：通过。

## OPS-01 数据库备份 —— 已修复
- 新增 `deploy/stocktrace-backup.sh`：先 `sqlite3 .backup`（在线一致；没有 CLI 时退回 python3 的 `sqlite3.backup`），
  再 `PRAGMA integrity_check`，然后 gzip，最后按份数轮转（`BACKUP_KEEP`，默认 14）。异地同步是可选的：
  设了 `BACKUP_RSYNC_TARGET` 就用 rsync，也可以用 `BACKUP_POST_HOOK`（例如 rclone）。任一步失败都以非 0 退出，异地同步失败也算失败。
  脚本先写 `.partial` 再改名，所以轮转永远删不到写了一半的文件。
- 新增 `deploy/stocktrace-backup.service`（oneshot，User=www，低 IO 优先级，可选 `/etc/stocktrace/backup.env`）
  和 `deploy/stocktrace-backup.timer`（每天 22:00 Asia/Shanghai，Persistent=true）。
- `.gitignore`：新增 `db.sqlite3-wal/-shm/-journal`、`*.sqlite3.bak*`、`backups/`、`*.sqlite3.gz`、
  `backend/scheduler_heartbeat.json`、`backend/logs/`。
- `DEPLOY.md`：第 2 节升级前的 `cp` 改成运行备份脚本。新增第 6 节「备份与恢复演练」，包括安装、恢复步骤和每月演练流程。
- 测试 `BackupScriptTests`（4 个）：备份能带上 -wal 里还没 checkpoint 的数据，轮转正确；源库缺失、钩子失败、KEEP 非法时都会失败退出。
- **运维须知**：`apt install sqlite3`；`mkdir /var/backups/stocktrace && chown www:www`；
  `cp` 两个 unit 到 /etc/systemd/system 后执行 `systemctl enable --now stocktrace-backup.timer`，然后手动跑一次确认。
  **异地目标需要用户自己提供**（NAS/对象存储/SSH key），没配之前只有同机备份。
- 未做：`PRAGMA auto_vacuum=INCREMENTAL` 和清理后执行 `incremental_vacuum`（属于改库行为和 scheduler 清理逻辑，第一次需要停服 VACUUM），留给后续。

## OPS-02 依赖锁定 —— 已修复（需在 VPS 核对）
- `backend/requirements.txt` 每个依赖都加了上界：`django>=6.0,<6.2`（代码基于 Django 6，线上是 6.x + Py3.12，
  所以**没有**用任务里给的 `<5.3`，因为那会让线上降级），`apscheduler>=3.10,<4`，`pandas>=2.2,<4`，DRF `<4`，cors `<5`，baostock `<1`，
  cryptography `<51`，requests `<3`，gunicorn `<27`；akshare 保持 `==1.18.64`。
- pandas 没有卡 `<3`：测试 venv 实测 pandas 3.0.6 全部测试通过，卡 `<3` 会让已验证的版本被降级。
  锁文件已经钉死 3.0.6，大版本漂移由锁文件挡住。
- 新增 `backend/requirements.lock`：由测试 venv 按 requirements.txt 的传递依赖闭包导出，共 38 个包，全部精确版本。
  没有生成 hash（`pip-compile --generate-hashes` 需要联网解析，而且会解析出最新版本，不是测试过的版本）。
- `requirements-dev.txt` 首行加了 `-r requirements.txt`。
- `DEPLOY.md`：安装命令改成 `pip install -r backend/requirements.lock`。新增第 7 节「依赖锁定与升级」，
  包括首次切换前用 `pip freeze` 和线上 diff，以及在全新 venv 里重新解析、测试、导出的流程。
- 测试 `DependencyPinTests`（3 个）：检查关键包有上界；锁文件全部是 `==`，且满足顶层范围；装的是 APScheduler 3.x。
- **VPS 需核对**：线上实际的 Django/pandas/numpy 版本可能和锁文件不同（锁文件是 Django 6.1.2、pandas 3.0.6、numpy 2.5.3）。
  第一次装锁文件之前先按第 7 节做 diff；如果线上是 pandas 2.x 且运行稳定，可以把锁文件改成线上版本，测试通过后再提交。

## REL-01 调度器移出 web worker —— 已修复
- `scheduler.py`：把任务注册拆成 `register_jobs()` / `build_scheduler()`，Web 内启动和独立进程共用这一份。
  调度器和每个 CronTrigger 都显式 `timezone=Asia/Shanghai`，每个 job 都是 `coalesce=True`、`max_instances=1`，并设 `misfire_grace_time`
  （intraday 240s，preopen 1800s，daily_summary/detector 3600s，post_close 900s；job_defaults 默认 900s）。
  新增 `run_forever(stop_event)`：拿不到锁时每 60s 重试一次并记 WARNING；退出时 `shutdown(wait=True)` 等在跑的任务结束。
  锁路径改为 `settings.STOCKTRACE_SCHEDULER_LOCK`（默认 `<STOCKTRACE_RUN_DIR>/scheduler.lock`，`STOCKTRACE_RUN_DIR` 默认 BASE_DIR），
  目录不存在时自动创建。`_SKIP_COMMANDS` 加了 `run_scheduler`。
- 新增 `stocks/management/commands/run_scheduler.py`：在前台运行，SIGTERM/SIGINT 会触发优雅退出；`STOCKTRACE_SCHEDULER=0` 时直接退出。
- `apps.py`：只有 `STOCKTRACE_RUN_SCHEDULER_IN_WEB=1` 时才在进程内启动（默认关闭）。启动失败改用 `logger.exception`，
  这样 APScheduler 4.x 的 ImportError 会带堆栈出现在日志里。附带效果：fetch_stock_data/backfill_* 等管理命令不会再抢调度锁（REL-03 的一部分）。
- `settings.py`：新增 `STOCKTRACE_RUN_DIR`、`STOCKTRACE_SCHEDULER_LOCK`。
- 两份 `gunicorn_config.py` 的 `max_requests` 从 500 改为 `GUNICORN_MAX_REQUESTS`，默认 20000（jitter 1000，0 = 不回收）。
- deploy：新增 `stocktrace-scheduler.service`（User=www，`RuntimeDirectory=stocktrace` + `RuntimeDirectoryPreserve=yes`，
  `STOCKTRACE_RUN_DIR=/run/stocktrace`，`TimeoutStopSec=300`，`MemoryMax=700M`）。
  `stocktrace.service` 加了 `Environment=STOCKTRACE_RUN_SCHEDULER_IN_WEB=0` 和 `STOCKTRACE_RUN_DIR=/run/stocktrace`。`env.example` 补了这几个变量。
  `DEPLOY.md` 的第 1、2、3 节改成双单元，并新增「调度器运行方式说明」。
- 测试 `SchedulerConfigTests`（5 个）和 `RunForeverTests`（3 个）。
- **部署步骤**：如果 `/etc/stocktrace/env` 里有 `STOCKTRACE_SCHEDULER=0` 或 `STOCKTRACE_RUN_SCHEDULER_IN_WEB=1`，先删掉
  （EnvironmentFile 会覆盖 unit 里的 Environment=）。然后 `cp stocktrace.service stocktrace-scheduler.service /etc/systemd/system/`，
  `daemon-reload`，`enable --now stocktrace-scheduler`，`restart stocktrace`，用 `journalctl -u stocktrace-scheduler | grep APScheduler`
  和 `curl 127.0.0.1:8000/api/health/` 确认。如果 VPS 上 `backend/gunicorn_config.py` 和 `deploy/gunicorn_config.py` 不是同一份，也要同步。
- **行为变化**：本地 `runserver` 默认不再跑定时任务。需要的话设 `STOCKTRACE_RUN_SCHEDULER_IN_WEB=1`，或者另开一个终端运行 `manage.py run_scheduler`。
- **局限/未做**：仍是 MemoryJobStore，调度进程**停机期间**错过的时间点重启后不会补跑（报告里的 SQLAlchemyJobStore 方案要新增依赖，这次没做）。
  `tasks.py` 里手动拉取用的 daemon 线程仍在 Web 进程里（不归本方向，属于 REL-04）。内存上限 700M 需要在 VPS 上观察峰值。

## REL-02 LOGGING + 健康检查 —— 已修复（告警推送只提供了 cron 示例）
- `settings.py` 新增 `LOGGING`：输出到 stdout，格式含时间、级别、模块、pid、线程。`stocks` 的级别由 `STOCKTRACE_LOGLEVEL` 控制（默认 INFO），
  `apscheduler` 和 `django.request` 是 WARNING。设了 `STOCKTRACE_LOG_FILE` 时会额外加一个 WatchedFileHandler 写文件。
- `views.py`（只加了日志）：`import logging` 和 `logger`；`_market_response`、`_market_bad_request`、`market_institutions` 三处 502 兜底各加一行 `logger.exception`。
  返回给前端的内容没改（脱敏属于另一条，SEC 方向）。
- 心跳（`scheduler.py`）：`attach_heartbeat()` 挂一个 60s 的心跳 job，同时监听 EXECUTED/ERROR/MISSED 事件，把 pid、模式、running、started_at、updated_at、
  各 job 的 next_run_time、各 job 最近一次结果（ok/error/missed，带时间）原子写到 `STOCKTRACE_SCHEDULER_HEARTBEAT`
  （默认 `<RUN_DIR>/scheduler_heartbeat.json`）。job 出错会记 ERROR，错过会记 WARNING。重启后保留上次的 last_runs，正常退出时把 running 写成 false。没有新增模型。
- 新增 `stocks/health.py`，并在 `stocks/urls.py` 加了 `health/` 路由（即 `/api/health/`）。它检查 DB（`SELECT 1`）、心跳是否新鲜
  （`STOCKTRACE_HEARTBEAT_STALE_SECONDS`，默认 180）、最近一次市场快照日期。正常返回 200，异常返回 503；`STOCKTRACE_SCHEDULER=0` 时调度状态不算失败。异常细节只写日志，不回显。
- `nginx-stocktrace.conf` 加了 `location = /api/health/`，只允许 127.0.0.1。`DEPLOY.md` 新增第 8 节，包括日志说明、health 说明和每 5 分钟一次的 cron 探测加推送示例。
- 本机实测：运行 run_scheduler 和 runserver，health 返回 200 且列出 5 个 job 的 next_run_time；停掉调度后变成 503。
- 测试 `HeartbeatAndHealthTests`（6 个）和 `LoggingConfigTests`（2 个）。
- **未做**：报告里提到的「APScheduler 事件出错就直接调 push_message 推送」没做（push 在 alerts.py，不归本方向），目前用 DEPLOY 第 8.3 节的 cron 加 STOCKTRACE_PUSH_URL 代替。
  health 返回 503 时 django.request 会记一条 ERROR，cron 每 5 分钟探测一次，调度停着时每 5 分钟会多一条日志，可以接受。

## 改动文件
自己负责的文件：`.gitignore`、`backend/StockTrace/settings.py`、`backend/stocks/urls.py`（只加了 health 路由）、`backend/stocks/scheduler.py`、
`backend/stocks/apps.py`、`backend/gunicorn_config.py`、`backend/requirements.txt`、`backend/requirements-dev.txt`、`backend/stocks/views.py`（只加了日志）、
`deploy/DEPLOY.md`、`deploy/env.example`、`deploy/gunicorn_config.py`、`deploy/nginx-stocktrace.conf`、`deploy/stocktrace.service`。
新增文件：`backend/requirements.lock`、`backend/stocks/health.py`、`backend/stocks/management/commands/run_scheduler.py`、`backend/stocks/tests_p0_ops.py`、
`deploy/stocktrace-backup.sh`、`deploy/stocktrace-backup.service`、`deploy/stocktrace-backup.timer`、`deploy/stocktrace-scheduler.service`。
`StockTrace/urls.py` 不需要改（`/api/` 已经 include 了 stocks.urls）。
合并注意：`scheduler.py` 和 `views.py` 其他方向也可能会改，冲突时以本分支的 `register_jobs` 结构为准，把对方新增的 job 移进 `register_jobs()`，
并补上 misfire/coalesce/timezone。`tests_p0_ops.py` 断言了 5 个 job id 的精确集合，新增 job 时要同步改这个测试。
