"""成分池日线历史回补（限速、单线程、可断点续跑）。

用法（建议在 VPS 上用 nohup / tmux 后台跑）：
    python manage.py backfill_pool_quotes                # 回补全部未回补的成分股
    python manage.py backfill_pool_quotes --limit 50     # 只处理 50 只
    python manage.py backfill_pool_quotes --interval 30  # 每只间隔 30 秒（默认 POOL_FETCH_INTERVAL_SECONDS）
    python manage.py backfill_pool_quotes --status       # 只看进度与预计耗时，不请求上游

说明：
- 每只拉 POOL_BACKFILL_CALENDAR_DAYS 自然日（默认 400 ≈ 270 个交易日）日 K，写 DailyQuote；
- 本地已有 ≥ POOL_MIN_BARS 根已收盘日线的个股直接跳过（标记回补完成，不请求上游）；
- 进度记在 PoolStockState：中断（Ctrl-C / 重启）后重跑同一命令即从断点继续；
- 与调度器的成分池采集任务共用 pool_quotes 锁，命令运行期间调度器任务自动让路，不会并发；
- 自选股不在回补范围（走原有抓取）。先执行 refresh_index_constituents 建成分表。
"""

from django.core.management.base import BaseCommand, CommandError

from stocks.pool import collector
from stocks.pool.constituents import pool_members
from stocks.task_lock import TaskLock


class Command(BaseCommand):
    help = '成分池（沪深300+中证500）日线历史回补：限速、单线程、可断点续跑'

    def add_arguments(self, parser):
        parser.add_argument('--interval', type=float, default=None,
                            help='两只之间的间隔秒数（默认 POOL_FETCH_INTERVAL_SECONDS，最少 5）')
        parser.add_argument('--limit', type=int, default=None, help='最多处理多少只（默认全部）')
        parser.add_argument('--with-increment', action='store_true',
                            help='同时处理收盘后增量（默认只回补；增量由调度器全天处理）')
        parser.add_argument('--status', action='store_true', help='只输出进度与预计耗时')

    def handle(self, *args, **options):
        if not pool_members():
            raise CommandError('成分表为空：先执行 python manage.py refresh_index_constituents')
        collector.sync_states()
        if options['status']:
            for key, value in collector.status_summary().items():
                self.stdout.write(f'{key}: {value}')
            return
        interval = options['interval']
        if interval is not None:
            interval = max(5.0, interval)
        modes = (collector.MODE_BACKFILL,)
        if options['with_increment']:
            modes = (collector.MODE_INCREMENT, collector.MODE_BACKFILL)

        lock = TaskLock('pool_quotes')
        if not lock.acquire(timeout=120):
            raise CommandError('pool_quotes 锁被占用（另一个回补命令在跑？），稍后重试')
        done = {'ok': 0, 'error': 0}

        def report(res):
            done['ok' if res.get('status') == 'ok' else 'error'] += 1
            n = done['ok'] + done['error']
            if res.get('status') == 'ok':
                self.stdout.write(f"[{n}] {res['code']} {res['mode']}：{res.get('rows')} 行，"
                                  f"库内 {res.get('bars')} 根，最新 {res.get('last')}")
            else:
                self.stderr.write(f"[{n}] {res['code']} {res['mode']} 失败，"
                                  f"{res.get('retry_in', '-')}s 后重试")

        try:
            collector.run_loop(modes=modes, limit=options['limit'], interval=interval, on_result=report)
        except KeyboardInterrupt:
            self.stdout.write('已中断：进度已保存，重跑同一命令即可继续')
        finally:
            lock.release()
        summary = collector.status_summary()
        self.stdout.write(f"本次成功 {done['ok']}、失败 {done['error']}；"
                          f"已回补 {summary['backfilled']}/{summary['pool_codes']}，"
                          f"剩余约 {summary['backfill_eta_hours']} 小时")
