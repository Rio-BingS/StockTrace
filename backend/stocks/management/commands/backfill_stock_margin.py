"""回补个股融资明细（StockMarginDaily），供探测器融资拥挤度维度使用。

用法（建议在 VPS 上执行）：
    python manage.py backfill_stock_margin --start 2026-01-01 [--end 2026-10-09]

说明：
- 沪深两所都只能按信用交易日逐日查询（akshare stock_margin_detail_sse/szse），
  每个交易日 2 次请求，间隔 --sleep；
- 按日期升序写入：深市偿还额由前一交易日余额推导，升序才推得出；
- 可续跑：两所都已落库的日期默认跳过（--force 覆盖）；某日某所无数据记为失败，
  有失败日时以非零状态退出，稍后用同一命令续跑只补缺的日期；
- 融资拥挤度只需最近 20 个交易日的数据；更长的历史仅供离线检验。
"""

import time
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError

from stocks import trading_calendar
from stocks.market import margin_stock

_FAILED_SHOWN = 20


def _parse_date(raw):
    try:
        return date.fromisoformat(raw)
    except ValueError as e:
        raise CommandError(f'日期格式应为 YYYY-MM-DD：{raw}') from e


class Command(BaseCommand):
    help = '回补个股融资明细（沪深交易所两融明细，按信用交易日逐日拉取）；可续跑'

    def add_arguments(self, parser):
        parser.add_argument('--start', required=True, help='起始日期 YYYY-MM-DD（含）')
        parser.add_argument('--end', help='结束日期 YYYY-MM-DD（含，默认最近已完成交易日的前一交易日）')
        parser.add_argument('--sleep', type=float, default=0.5, help='每次请求间隔秒数（默认 0.5）')
        parser.add_argument('--force', action='store_true', help='已落库的日期也重新拉取覆盖')

    def handle(self, *args, **options):
        start = _parse_date(options['start'])
        if options.get('end'):
            end = _parse_date(options['end'])
        else:
            latest = trading_calendar.expected_latest_date() or date.today()
            # T+1 披露：最近完成交易日的明细要到下一交易日早间才有
            end = latest - timedelta(days=1)
        if start > end:
            raise CommandError(f'--start {start} 晚于 --end {end}')
        sleep_s = max(0.0, options['sleep'])

        days = self._trading_days(start, end)
        if not days:
            self.stdout.write('区间内没有交易日，无需回补')
            return
        done = set() if options['force'] else margin_stock.stock_margin_days_present(days[0], days[-1])
        todo = [d for d in days if d not in done]
        self.stdout.write(f'区间 {start} ~ {end}：交易日 {len(days)} 个，已有 {len(done)} 个，待补 {len(todo)} 个')

        failed = []
        written = 0
        for i, day in enumerate(todo, 1):
            counts = {}
            for kind in ('sse', 'szse'):
                n = margin_stock.save_stock_margin_day(kind, day)
                counts[kind] = n
                if sleep_s:
                    time.sleep(sleep_s)
            if not all(counts.values()):
                missing = '、'.join(k for k, n in counts.items() if not n)
                failed.append((day, f'{missing} 无数据'))
            written += sum(counts.values())
            self.stdout.write(f'[{i}/{len(todo)}] {day} 沪 {counts["sse"]} 行 / 深 {counts["szse"]} 行')

        self.stdout.write(f'完成：写入 {written} 行，失败 {len(failed)} 天，跳过 {len(done)} 天')
        if failed:
            for day, why in failed[:_FAILED_SHOWN]:
                self.stdout.write(f'  失败 {day}：{why}')
            if len(failed) > _FAILED_SHOWN:
                self.stdout.write(f'  ……另有 {len(failed) - _FAILED_SHOWN} 天')
            raise CommandError(f'{len(failed)} 个交易日未补齐，可稍后用同一命令续跑')

    def _trading_days(self, start, end):
        """区间内的交易日；日历未知（None）的工作日也尝试（未披露会如实记失败）。"""
        days, day = [], start
        while day <= end:
            try:
                ok = trading_calendar.is_trading_day(day)
            except Exception as e:  # noqa: BLE001 命令边界：日历异常中止并提示原因
                raise CommandError(f'交易日历查询异常：{e}') from e
            if ok is not False:
                days.append(day)
            day += timedelta(days=1)
        return days
