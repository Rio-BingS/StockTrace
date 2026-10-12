"""回补限售解禁日程（StockRestrictedRelease，东财 RPT_LIFT_STAGE）。

用法（建议在 VPS 上执行）：
    python manage.py backfill_restricted_release [--start 2018-01-01] [--end 2026-12-31]

说明：
- 默认区间 2018-01-01 ~ 今天+60 天（含已排期的未来解禁）；
- 按季度切片拉取，每片整段覆盖库里解禁日落在该片内的行（与每日任务同口径）；
- 某片拉取不完整时不动该片的旧数据并记失败，其余切片照常；有失败时以非零
  状态退出，重跑同一命令即可（覆盖写，天然幂等）。
"""

import time
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from stocks.market import restricted_release as rr

DEFAULT_START = date(2018, 1, 1)


def _parse_date(raw):
    try:
        return date.fromisoformat(raw)
    except ValueError as e:
        raise CommandError(f'日期格式应为 YYYY-MM-DD：{raw}') from e


def quarter_slices(start, end):
    """[start, end] 按自然季度切片，返回 [(片起, 片止)]（首尾片按区间截断）。"""
    out, cur = [], start
    while cur <= end:
        q_end_month = ((cur.month - 1) // 3 + 1) * 3
        nxt = date(cur.year + (q_end_month == 12), q_end_month % 12 + 1, 1)
        seg_end = min(end, nxt - timedelta(days=1))
        out.append((cur, seg_end))
        cur = seg_end + timedelta(days=1)
    return out


class Command(BaseCommand):
    help = '回补限售解禁日程（东财 RPT_LIFT_STAGE，按季度切片整段覆盖）'

    def add_arguments(self, parser):
        parser.add_argument('--start', default=DEFAULT_START.isoformat(),
                            help='起始解禁日 YYYY-MM-DD（含，默认 2018-01-01）')
        parser.add_argument('--end', help='结束解禁日 YYYY-MM-DD（含，默认今天+60 天）')
        parser.add_argument('--sleep', type=float, default=0.5, help='切片间隔秒数（默认 0.5）')

    def handle(self, *args, **options):
        start = _parse_date(options['start'])
        end = _parse_date(options['end']) if options.get('end') else (
            timezone.localdate() + timedelta(days=rr.REFRESH_AHEAD_DAYS)
        )
        if start > end:
            raise CommandError(f'--start {start} 晚于 --end {end}')
        sleep_s = max(0.0, options['sleep'])
        slices = quarter_slices(start, end)
        total, failed = 0, []
        for i, (a, b) in enumerate(slices, 1):
            try:
                n = rr.replace_window(a, b, rr.fetch_releases(a, b))
            except rr.ReleaseFetchError as e:
                failed.append((a, b, str(e)))
                self.stderr.write(f'[{i}/{len(slices)}] {a} ~ {b} 失败：{e}')
                continue
            total += n
            self.stdout.write(f'[{i}/{len(slices)}] {a} ~ {b}：{n} 行')
            if sleep_s and i < len(slices):
                time.sleep(sleep_s)
        self.stdout.write(f'完成：写入 {total} 行，失败 {len(failed)} 片')
        if failed:
            raise CommandError(f'{len(failed)} 个季度切片未完成，可稍后用同一命令重跑')
