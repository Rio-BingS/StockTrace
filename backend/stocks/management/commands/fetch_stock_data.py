"""
手动触发股票数据拉取的管理命令

用法：
    python manage.py fetch_stock_data 000001     # 拉取单只股票
    python manage.py fetch_stock_data --all       # 拉取所有关注股票
    python manage.py fetch_stock_data 000001 --days 3   # 分钟线回溯 3 个交易日（上限 5）
    python manage.py fetch_stock_data --all --days 5    # 所有关注股票，分钟线回溯 5 个交易日
    # 日线全量重拉（修复复权断层/成交量单位存量，DAT-02/DAT-03），可限定单一数据源：
    python manage.py fetch_stock_data --all --full-daily --source sina

有失败、跳过（任务锁被占用）或参数错误时以非零退出码结束，便于脚本/cron 判断（REL-09）。
"""
import time

from django.core.management.base import BaseCommand, CommandError
from stocks.models import Stock
from stocks.datasources import DATA_SOURCES
from stocks.ingest import fetch_daily_data
from stocks.jobs import fetch_stock_all_data
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
            '--days', type=int, default=None,
            help='分钟线回溯的交易日数，上限 5（日线始终增量补齐）。'
                 '缺省：单只 5 个交易日；--all 为轻量模式 2 个交易日',
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
        """返回失败只数（异常或 0 条都算失败）。"""
        sources = [s for s in DATA_SOURCES if s.name == source_name] if source_name else None
        failed = 0
        for stock in stocks:
            try:
                n = fetch_daily_data(stock, full=True, sources=sources)
            except Exception as e:  # noqa: BLE001  单只失败不影响其余
                self.stdout.write(self.style.ERROR(f"  {stock.code}: 失败 - {e}"))
                failed += 1
                continue
            if not n:
                failed += 1
            style = self.style.SUCCESS if n else self.style.ERROR
            self.stdout.write(style(f"  {stock.code}: 全量重拉 {n} 条日K线"))
        return failed

    def _fail(self, message):
        # 同时写 stdout（兼容只看输出的用法）并以非零退出码结束（REL-09）
        self.stdout.write(self.style.ERROR(message))
        raise CommandError(message)

    def handle(self, *args, **options):
        days = options['days']
        if days is not None and not 1 <= days <= 5:
            raise CommandError('--days 须在 1–5 之间（分钟线回溯的交易日数）')
        # 与定时任务、Web 手动拉取共用 stock_quotes 任务锁，避免并发写同一批行情（REL-04）
        with task_lock('stock_quotes', timeout=_LOCK_WAIT_SECONDS) as ok:
            if ok:
                self._handle(**options)
        if not ok:
            self._fail(f'行情拉取任务锁被占用（已等待 {_LOCK_WAIT_SECONDS}s），请稍后再试')

    def _fetch_all(self, days):
        """拉取所有关注股票；返回失败只数。传了 --days 则按其回溯分钟线，否则轻量模式。"""
        failed = 0
        for stock in Stock.objects.filter(is_active=True):
            try:
                if days is None:
                    count = fetch_stock_all_data(stock, light=True)
                else:
                    count = fetch_stock_all_data(stock, days_back=days)
            except Exception as e:  # noqa: BLE001  单只失败不影响其余
                failed += 1
                self.stdout.write(self.style.ERROR(f"  {stock.code}: 失败 - {e}"))
            else:
                self.stdout.write(self.style.SUCCESS(f"  {stock.code}: 成功，{count} 条日K线"))
            time.sleep(1)
        return failed

    def _handle(self, **options):
        if options['source'] and not options['full_daily']:
            self._fail('--source 仅能与 --full-daily 一起使用')
        if options['full_daily']:
            if options['all']:
                stocks = list(Stock.objects.filter(is_active=True))
            elif options['code']:
                stocks = list(Stock.objects.filter(code=options['code']))
                if not stocks:
                    self._fail(f"股票 {options['code']} 不在关注列表中")
            else:
                self._fail('请指定股票代码或使用 --all')
            self.stdout.write(f'正在全量重拉 {len(stocks)} 只股票日线...')
            failed = self._full_daily(stocks, options['source'])
            if failed:
                raise CommandError(f'{failed}/{len(stocks)} 只股票全量重拉失败')
            return

        if options['all']:
            self.stdout.write('正在拉取所有关注股票数据...')
            failed = self._fetch_all(options['days'])
            if failed:
                raise CommandError(f'{failed} 只股票拉取失败')
        elif options['code']:
            code = options['code']
            try:
                stock = Stock.objects.get(code=code)
            except Stock.DoesNotExist:
                self._fail(f"股票 {code} 不在关注列表中")

            self.stdout.write(f'正在拉取 {stock.code} {stock.name} 数据...')
            try:
                count = fetch_stock_all_data(stock, days_back=options['days'] or 5)
            except Exception as e:  # noqa: BLE001
                self._fail(f'{stock.code}: 失败 - {e}')
            self.stdout.write(
                self.style.SUCCESS(f"完成，{count} 条日K线数据")
            )
        else:
            self._fail('请指定股票代码或使用 --all')
