"""回填上交所 ETF 历史份额到日度快照。

用法（建议在 VPS 上执行）：
    python manage.py backfill_etf_share_sse --days 130

说明：
- 数据源 akshare.fund_etf_scale_sse（上交所官方，任意历史日期可取，仅沪市）；
  深交所对应接口上游损坏，深市 ETF 只能由收盘任务向前积累；
- 已有 ETF 份额快照的日期一律跳过（本站收盘快照含市值/成交额，不覆盖）；
- 每个交易日一次请求，间隔默认 0.5s；重跑幂等。
"""

import time
from datetime import timedelta

from django.core.management.base import BaseCommand

from stocks import trading_calendar
from stocks.market import snapshots
from stocks.models import MarketDailySnapshot

# 日历回看自然日上限：交易日数 × 2 + 缓冲（长假最多把 130 交易日拉宽到约 190 自然日）
_CALENDAR_CAP_MULTIPLIER = 2
_CALENDAR_CAP_BUFFER = 30


class Command(BaseCommand):
    help = '回填上交所 ETF 历史份额到日度快照（仅沪市；深市只能向前积累）'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=130, help='回填最近 N 个交易日（默认 130）')
        parser.add_argument('--sleep', type=float, default=0.5, help='交易日间隔秒数（默认 0.5）')

    def handle(self, *args, **options):
        n_days = options['days']
        sleep_s = options['sleep']
        end_day = trading_calendar.expected_latest_date()
        if end_day is None:
            self.stderr.write('交易日历不可用，中止')
            return

        # 往回收集 n_days 个交易日（自设自然日上限，绕开 _recent_trading_days 的 120 自然日研究窗口上限）
        days = []
        day = end_day
        calendar_cap = n_days * _CALENDAR_CAP_MULTIPLIER + _CALENDAR_CAP_BUFFER
        for _ in range(calendar_cap):
            try:
                if trading_calendar.is_trading_day(day):
                    days.append(day)
                    if len(days) == n_days:
                        break
            except Exception as e:  # noqa: BLE001 命令边界：日历任何异常都中止回补并提示原因
                self.stderr.write(f'交易日历查询异常，中止: {e}')
                return
            day -= timedelta(days=1)
        if not days:
            self.stderr.write('未取得任何交易日，中止')
            return

        written = skipped = failed = 0
        for i, trade_day in enumerate(days, 1):
            if MarketDailySnapshot.objects.filter(
                kind=MarketDailySnapshot.KIND_ETF_SHARE, trade_date=trade_day,
            ).exists():
                skipped += 1
                continue
            rows = snapshots.fetch_sse_share_rows(trade_day)
            if not rows:
                failed += 1
                self.stdout.write(f'[{i}/{len(days)}] {trade_day} 无数据，不写快照')
                time.sleep(sleep_s)
                continue
            MarketDailySnapshot.objects.update_or_create(
                kind=MarketDailySnapshot.KIND_ETF_SHARE, trade_date=trade_day,
                defaults={'payload': rows},
            )
            written += 1
            self.stdout.write(f'[{i}/{len(days)}] {trade_day} 写入 {len(rows)} 行')
            time.sleep(sleep_s)

        self.stdout.write(self.style.SUCCESS(
            f'回填完成：写入 {written} 天，已有快照跳过 {skipped} 天，无数据 {failed} 天'
        ))
