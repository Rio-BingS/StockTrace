"""手动触发探测器扫描。

为什么提供命令：17:15 的定时任务之外，部署验证、排查与演示都需要能
手动跑一次；同日重跑幂等，重复执行安全：信号行按对象+日期覆盖，本轮
确定无信号的对象删除当日未回看的旧信号（数据降级的对象保留旧行），
run 行的调参流水与回看计数按当日累加而不是被覆盖；落库在单个事务内。
非交易日/收盘前调用会返回 skipped 原因（守卫与定时任务同一套，不提供
绕过开关——非交易日落库会产生脏日期数据，2026-09-07 踩过的坑）。

用法：
    DJANGO_DEBUG=true python manage.py run_detector_scan
"""

import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = '手动触发探测器日度扫描（同日重跑幂等；非交易日/收盘前如实跳过）'

    def handle(self, *args, **options):
        from stocks.detector.scan import run_scan

        summary = run_scan()
        self.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
