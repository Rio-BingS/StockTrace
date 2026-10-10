"""独立运行 APScheduler 定时任务（REL-01）。

为什么单独成进程：调度器原先寄生在唯一的 Gunicorn worker 里，worker 每次被
max_requests 回收/重启都会在 graceful_timeout 后强杀正在跑的拉取/扫描任务，
并让恰好落在重启窗口里的定时点被判 misfire 跳过。独立进程只在部署时重启，
退出时等待在跑任务结束。

用法（前台阻塞，由 systemd 单元 stocktrace-scheduler 托管）：
    python manage.py run_scheduler
STOCKTRACE_SCHEDULER=0 仍是全局总开关：设置后本命令直接退出、不启动调度。
"""

import os
import signal
import threading

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = '前台运行定时任务调度器（生产由 systemd stocktrace-scheduler.service 托管）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--lock-retry', type=int, default=60,
            help='调度锁被占用时的重试间隔秒数（默认 60）',
        )

    def handle(self, *args, **options):
        from stocks.scheduler import run_forever

        if os.environ.get('STOCKTRACE_SCHEDULER', '1').lower() in ('0', 'false', 'no', 'off'):
            self.stderr.write('STOCKTRACE_SCHEDULER 已禁用调度，run_scheduler 不启动')
            return

        stop_event = threading.Event()

        def _on_signal(signum, frame):
            stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, _on_signal)

        run_forever(stop_event, lock_retry_seconds=max(1, options['lock_retry']))
