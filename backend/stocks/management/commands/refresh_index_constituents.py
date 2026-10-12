"""手动刷新成分池（沪深300 + 中证500 当前成分）。

用法：
    python manage.py refresh_index_constituents [--index 000300]

调度器每周一 08:30 自动刷新一次；本命令用于首次部署或成分调整后立即刷新。
某指数全部数据源失败时保留旧成分，以非零状态退出，重跑即可。
"""

from django.core.management.base import BaseCommand, CommandError

from stocks.pool import constituents


class Command(BaseCommand):
    help = '刷新沪深300、中证500 当前成分（失败保留旧值）'

    def add_arguments(self, parser):
        parser.add_argument('--index', action='append', dest='indices',
                            choices=[c for c, _n, _k in constituents.POOL_INDICES],
                            help='只刷新指定指数（可重复），默认全部')

    def handle(self, *args, **options):
        result = constituents.refresh_constituents(options.get('indices'))
        failed = []
        for index_code, info in result.items():
            if info['status'] == 'ok':
                self.stdout.write(
                    f"{index_code}: {info['count']} 只（{info['source']}），"
                    f"新增 {info['added']}、移出 {info['removed']}")
            else:
                failed.append(index_code)
                self.stderr.write(f"{index_code}: 刷新失败，保留旧值 {info['count']} 只：{info['error']}")
        self.stdout.write(f'成分池合计 {len(constituents.pool_members())} 只')
        if failed:
            raise CommandError(f"{'、'.join(failed)} 刷新失败（已保留旧值），可稍后重跑")
