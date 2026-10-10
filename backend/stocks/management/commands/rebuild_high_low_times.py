"""
用已入库的分钟 bar 按交易日重算 DailyQuote.high_time / low_time（DAT-01 回刷）

背景：新浪分钟线兜底曾不按日期过滤，按约 8 个交易日求极值并写进错误交易日；
MinuteBar 按 datetime 入库的部分本身是对的，可以直接据此修复。

用法：
    python manage.py rebuild_high_low_times --dry-run          # 只统计将改动的行
    python manage.py rebuild_high_low_times                    # 全部自选股、全部日期
    python manage.py rebuild_high_low_times --code 000001 --since 2026-09-01
"""

from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from stocks.models import DailyQuote, MinuteBar, Stock
from stocks.services import compute_high_low_times


class Command(BaseCommand):
    help = '用已有分钟K线按交易日重算日线最高/最低点时间'

    def add_arguments(self, parser):
        parser.add_argument('--code', type=str, help='只处理该股票代码')
        parser.add_argument('--since', type=str, help='只处理该日期（YYYY-MM-DD）及之后的交易日')
        parser.add_argument('--dry-run', action='store_true', help='只统计，不写库')

    def handle(self, *args, **options):
        since = None
        if options.get('since'):
            try:
                since = datetime.strptime(options['since'], '%Y-%m-%d').date()
            except ValueError as e:
                raise CommandError('--since 格式应为 YYYY-MM-DD') from e

        stocks = Stock.objects.all().order_by('code')
        if options.get('code'):
            stocks = stocks.filter(code=options['code'])
            if not stocks.exists():
                raise CommandError(f"股票 {options['code']} 不存在")

        dry_run = options['dry_run']
        total = changed = no_bars = 0
        for stock in stocks:
            quotes = DailyQuote.objects.filter(stock=stock)
            if since:
                quotes = quotes.filter(trade_date__gte=since)
            quotes = list(quotes.values_list('id', 'trade_date', 'high_time', 'low_time'))
            if not quotes:
                continue

            bars = MinuteBar.objects.filter(stock=stock)
            if since:
                bars = bars.filter(datetime__gte=timezone.make_aware(
                    datetime.combine(since, datetime.min.time())))
            # 按本地交易日分组
            by_day = {}
            for dt, high, low in bars.order_by('datetime').values_list(
                    'datetime', 'high', 'low').iterator():
                day = timezone.localtime(dt).date()
                by_day.setdefault(day, []).append((dt, high, low))

            stock_changed = 0
            for qid, trade_date, old_high, old_low in quotes:
                total += 1
                day_bars = by_day.get(trade_date)
                if not day_bars:
                    no_bars += 1
                    continue
                hl = compute_high_low_times(day_bars)
                if hl is None:
                    no_bars += 1
                    continue
                high_time, low_time = hl[0], hl[1]
                if high_time == old_high and low_time == old_low:
                    continue
                stock_changed += 1
                if not dry_run:
                    DailyQuote.objects.filter(id=qid).update(
                        high_time=high_time, low_time=low_time)
            changed += stock_changed
            if stock_changed:
                self.stdout.write(f'  {stock.code}: {stock_changed} 个交易日高低点时间'
                                  f"{'将被' if dry_run else '已'}更正")

        prefix = '[dry-run] ' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(
            f'{prefix}检查 {total} 个交易日，更正 {changed} 个，'
            f'无分钟数据跳过 {no_bars} 个'
        ))

