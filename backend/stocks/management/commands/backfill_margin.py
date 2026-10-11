"""回补沪深两融（融资余额）日度快照，供探测器融资余额维度使用（#9）。

用法（建议在 VPS 上执行）：
    python manage.py backfill_margin --start 2025-06-01 --end 2026-10-09

说明：
- 沪市：上交所 stock_margin_sse 按区间批量返回历史，按 --chunk-days 分段拉取；
- 深市：深交所 stock_margin_szse 只能按日查询，每个交易日一次请求，间隔 --sleep；
- 只写沪深同日齐全的行（与收盘任务 save_margin_snapshot 同一口径与 payload）；
- 失败可续跑：每天单独落库，已有且两市融资余额齐全的日期默认跳过（--force 覆盖），
  中途失败或某日深市无数据，重跑同一命令只补缺的日期；有失败日时以非零状态退出。
"""

import time
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError

from stocks import trading_calendar
from stocks.market import sentiment, snapshots
from stocks.models import MarketDailySnapshot

# 失败日期在输出里最多列这么多个（其余只计数）
_FAILED_SHOWN = 20


def _parse_date(raw):
    try:
        return date.fromisoformat(raw)
    except ValueError as e:
        raise CommandError(f'日期格式应为 YYYY-MM-DD：{raw}') from e


class Command(BaseCommand):
    help = '回补沪深两融（融资余额）日度快照：沪市批量历史，深市按日拉取；可续跑'

    def add_arguments(self, parser):
        parser.add_argument('--start', required=True, help='起始日期 YYYY-MM-DD（含）')
        parser.add_argument('--end', help='结束日期 YYYY-MM-DD（含，默认最近已完成交易日）')
        parser.add_argument('--sleep', type=float, default=0.5, help='深市逐日请求间隔秒数（默认 0.5）')
        parser.add_argument('--chunk-days', type=int, default=90, help='沪市每段拉取的自然日数（默认 90）')
        parser.add_argument('--force', action='store_true', help='已有快照也重新拉取覆盖')

    def handle(self, *args, **options):
        start = _parse_date(options['start'])
        end = _parse_date(options['end']) if options.get('end') else (
            trading_calendar.expected_latest_date() or date.today()
        )
        if start > end:
            raise CommandError(f'--start {start} 晚于 --end {end}')
        chunk = max(1, options['chunk_days'])
        sleep_s = max(0.0, options['sleep'])

        days = self._trading_days(start, end)
        if not days:
            self.stdout.write('区间内没有交易日，无需回补')
            return
        done = self._complete_days(days) if not options['force'] else set()
        todo = [d for d in days if d not in done]
        self.stdout.write(f'区间 {start} ~ {end}：交易日 {len(days)} 个，已有 {len(done)} 个，待补 {len(todo)} 个')
        if not todo:
            return

        sh_rows = self._fetch_sh(todo[0], todo[-1], chunk)
        written, failed = 0, []
        for i, day in enumerate(todo, 1):
            sh_row = sh_rows.get(day.strftime('%Y%m%d'))
            if not sh_row:
                failed.append((day, '沪市无数据'))
                continue
            sz_row = sentiment._sz_margin_row(day)
            if sleep_s:
                time.sleep(sleep_s)
            if not sz_row or sz_row.get('total') is None:
                failed.append((day, '深市无数据'))
                continue
            row = snapshots.write_margin_snapshot(day, sh_row, sz_row)
            written += 1
            self.stdout.write(f'[{i}/{len(todo)}] {day} 融资余额 {row["rz"]} 亿')

        self.stdout.write(f'完成：写入 {written}，失败 {len(failed)}，跳过 {len(done)}')
        if failed:
            for day, why in failed[:_FAILED_SHOWN]:
                self.stdout.write(f'  失败 {day}：{why}')
            if len(failed) > _FAILED_SHOWN:
                self.stdout.write(f'  ……另有 {len(failed) - _FAILED_SHOWN} 天')
            raise CommandError(f'{len(failed)} 个交易日未补齐，可稍后用同一命令续跑')

    def _trading_days(self, start, end):
        """区间内的交易日；日历未知（None）的工作日也尝试（深市未披露会如实记失败）。"""
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

    def _complete_days(self, days):
        """已有且融资余额两市齐全的快照日期（新版 payload 带 sh_rz/sz_rz）。

        旧版行没有 sz_rz、且单边缺失时按 0 相加，无法确认 rz 可信，回补时重拉覆盖一次。
        """
        rows = MarketDailySnapshot.objects.filter(
            kind=MarketDailySnapshot.KIND_MARGIN, trade_date__gte=days[0], trade_date__lte=days[-1],
        ).values_list('trade_date', 'payload')
        return {
            d for d, payload in rows
            if payload and payload[0].get('sh_rz') is not None and payload[0].get('sz_rz') is not None
        }

    def _fetch_sh(self, start, end, chunk):
        """分段批量拉沪市历史，合并为 {yyyymmdd: row}；单段失败只影响该段日期。"""
        out, seg = {}, start
        while seg <= end:
            seg_end = min(end, seg + timedelta(days=chunk - 1))
            try:
                rows = sentiment._sh_margin_rows(start=seg, end=seg_end)
            except Exception as e:  # noqa: BLE001 上游边界：本段失败，相关日期记失败可续跑
                self.stderr.write(f'沪市 {seg} ~ {seg_end} 拉取失败：{e}')
                rows = {}
            out.update(rows or {})
            seg = seg_end + timedelta(days=1)
        return out
