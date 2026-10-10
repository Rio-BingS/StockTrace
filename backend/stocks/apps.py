from django.apps import AppConfig
import logging
import os

logger = logging.getLogger(__name__)

_TRUTHY = ('1', 'true', 'yes', 'on')


def run_scheduler_in_web():
    """是否在 Web/当前进程内启动调度器（REL-01：默认关闭，生产改用 manage.py run_scheduler）。"""
    return os.environ.get('STOCKTRACE_RUN_SCHEDULER_IN_WEB', '0').strip().lower() in _TRUTHY


class StocksConfig(AppConfig):
    name = 'stocks'
    verbose_name = '股票监控'

    def ready(self):
        """仅在 STOCKTRACE_RUN_SCHEDULER_IN_WEB=1 时于进程内启动 APScheduler（兼容旧部署）。

        默认不启动：生产由独立的 run_scheduler 进程负责调度；这样其他管理命令
        （fetch_stock_data、backfill_* 等）也不会再意外抢走调度锁。
        """
        if not run_scheduler_in_web():
            return
        try:
            from .scheduler import start
            start()
        except Exception:
            # ImportError（如 APScheduler 漂移到 4.x）等必须带堆栈可见，不能只是一行 warning
            logger.exception("APScheduler 启动失败")
