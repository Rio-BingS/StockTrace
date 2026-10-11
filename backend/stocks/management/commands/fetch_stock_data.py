"""
手动触发股票数据拉取的管理命令

用法：
    python manage.py fetch_stock_data 000001     # 拉取单只股票
    python manage.py fetch_stock_data --all       # 拉取所有关注股票
    python manage.py fetch_stock_data 000001 --days 60  # 指定历史天数
    # 日线全量重拉（修复复权断层/成交量单位存量，DAT-02/DAT-03），可限定单一数据源：
    python manage.py fetch_stock_data --all --full-daily --source sina
"""

from django.core.management.base import BaseCommand
from stocks.models import Stock
from stocks.services import (
    DATA_SOURCES, fetch_all_active_stocks, fetch_daily_data, fetch_stock_all_data,
)
from stocks.task_lock import task_lock

_SOURCE_NAMES = [s.name for s in DATA_SOURCES]
_LOCK_WAIT_SECONDS = 600


class Command(BaseCommand):
    help = '拉取股票数据（日K线 + 分钟K线）'

    def add_arguments(self, parser):
        parser.add_argument(
            'code', type=str, nargs='?', help='股票代码（如 000001）'
        )
        parser.add_argument(
            '--all', action='store_true', help='拉取所有关注股票'
        )
        parser.add_argument(
            '--days', type=int, default=5, help='分钟数据回溯天数（默认5天）'
        )
        parser.add_argument(
            '--full-daily', action='store_true',
            help='只做日线全量重拉（库内最早日期起，至少一年），重算昨收/涨跌幅',
        )
        parser.add_argument(
            '--source', choices=_SOURCE_NAMES,
            help='配合 --full-daily：只用指定单一数据源（存量修复建议 sina）',
        )

    def _full_daily(self, stocks, source_name):
        sources = [s for s in DATA_SOURCES if s.name == source_name] if source_name else None
        for stock in stocks:
            try:
                n = fetch_daily_data(stock, full=True, sources=sources)
            except Exception as e:  # noqa: BLE001  单只失败不影响其余
                self.stdout.write(self.style.ERROR(f"  {stock.code}: 失败 - {e}"))
                continue
            style = self.style.SUCCESS if n else self.style.ERROR
            self.stdout.write(style(f"  {stock.code}: 全量重拉 {n} 条日K线"))

    def handle(self, *args, **options):
        # 与定时任务、Web 手动拉取共用 stock_quotes 任务锁，避免并发写同一批行情（REL-04）
        with task_lock('stock_quotes', timeout=_LOCK_WAIT_SECONDS) as ok:
            if not ok:
                self.stdout.write(self.style.ERROR(
                    f'行情拉取任务锁被占用（已等待 {_LOCK_WAIT_SECONDS}s），请稍后再试'))
                return
            self._handle(**options)

    def _handle(self, **options):
        if options['source'] and not options['full_daily']:
            self.stdout.write(self.style.ERROR('--source 仅能与 --full-daily 一起使用'))
            return
        if options['full_daily']:
            if options['all']:
                stocks = list(Stock.objects.filter(is_active=True))
            elif options['code']:
                stocks = list(Stock.objects.filter(code=options['code']))
                if not stocks:
                    self.stdout.write(self.style.ERROR(f"股票 {options['code']} 不在关注列表中"))
                    return
            else:
                self.stdout.write(self.style.ERROR('请指定股票代码或使用 --all'))
                return
            self.stdout.write(f'正在全量重拉 {len(stocks)} 只股票日线...')
            self._full_daily(stocks, options['source'])
            return

        if options['all']:
            self.stdout.write('正在拉取所有关注股票数据...')
            results = fetch_all_active_stocks()
            for r in results:
                if r['status'] == 'success':
                    self.stdout.write(
                        self.style.SUCCESS(f"  {r['code']}: 成功，{r.get('count', 0)} 条日K线")
                    )
                else:
                    self.stdout.write(
                        self.style.ERROR(f"  {r['code']}: 失败 - {r.get('message', '')}")
                    )
        elif options['code']:
            code = options['code']
            try:
                stock = Stock.objects.get(code=code)
            except Stock.DoesNotExist:
                self.stdout.write(self.style.ERROR(f"股票 {code} 不在关注列表中"))
                return

            self.stdout.write(f'正在拉取 {stock.code} {stock.name} 数据...')
            count = fetch_stock_all_data(stock, days_back=options['days'])
            self.stdout.write(
                self.style.SUCCESS(f"完成，{count} 条日K线数据")
            )
        else:
            self.stdout.write(self.style.ERROR('请指定股票代码或使用 --all'))
