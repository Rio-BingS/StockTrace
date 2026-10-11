"""回填大盘主力资金流日度快照（market_ff），并提供快照巡检。

用法（建议在 VPS 上跑，本地代理对 push2his.eastmoney.com 干扰严重）：
    python manage.py backfill_market_snapshots              # 只补缺失日期
    python manage.py backfill_market_snapshots --force      # 覆盖已有行（慎用）
    python manage.py backfill_market_snapshots --audit      # 只读巡检疑似污染行
    python manage.py backfill_market_snapshots --audit --delete-suspect   # 巡检并删除

口径纪律（DAT-06）：
- 只接受东财主源 eastmoney_market_fund_flow（ak.stock_market_fund_flow，
  沪深两市合计、单位元），与收盘任务写入的 market_ff 同口径；北向兜底源
  （stock_hsgt_hist_em，亿元、北向净买额口径）一律拒绝写库；
- 默认只补缺不覆盖：收盘任务自采的两市合计行优先，--force 才覆盖；
- 只写最近一个已完成交易日（含）之前的日期，不把盘中未收盘数值落库；
- 行业资金流回填已停用：当日快照为同花顺口径，上游历史只有东财口径
  （stock_sector_fund_flow_hist），名称体系与「主力」定义都不同，混写会污染
  多日轮动指标；行业 20d 只靠收盘任务向前积累。概念/ETF 份额同样只能向前积累。
"""

from datetime import date as date_cls, timedelta

import pandas as pd
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from stocks import trading_calendar
from stocks.market import snapshots
from stocks.models import MarketDailySnapshot

# 巡检删除只针对「确定是错口径/错单位」的原因；parts_all_none 只提示不删
_DELETABLE_REASONS = {'empty_payload', 'main_net_missing', 'magnitude', 'untrusted_src', 'bad_unit'}


class Command(BaseCommand):
    help = '回填大盘主力资金流快照（仅东财主源、只补缺）；--audit 巡检疑似污染行'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=130, help='回补最近 N 天上游历史（默认 130）')
        parser.add_argument('--sleep', type=float, default=0.5, help='（已停用，保留兼容旧调用）')
        parser.add_argument('--force', action='store_true', help='覆盖已有 market_ff 行（默认只补缺）')
        parser.add_argument('--audit', action='store_true', help='只读巡检 market_ff 疑似污染行，不回填')
        parser.add_argument(
            '--delete-suspect', action='store_true',
            help='配合 --audit：删除确定为错口径/错单位的行（默认只报告）',
        )

    def handle(self, *args, **options):
        if options['delete_suspect'] and not options['audit']:
            self.stderr.write('--delete-suspect 必须与 --audit 一起使用')
            return
        if options['audit']:
            self._audit(delete=options['delete_suspect'])
            return
        self.stdout.write(self.style.WARNING(
            '行业资金流回填已停用（同花顺当日口径与东财历史口径不一致），仅回补大盘主力资金流'
        ))
        self._backfill_market_ff(options['days'], options['force'])

    def _backfill_market_ff(self, days, force):
        from stocks.market import flows

        try:
            hist = flows.fetch_market_fund_flow_hist(days=days, force=True)
        except Exception as e:
            self.stderr.write(f'大盘资金流历史回补失败（上游恢复后重跑即可）: {e}')
            return
        hist = hist or {}
        source = hist.get('source')
        if source != flows.MARKET_FUND_SOURCE_EM:
            # 北向兜底/多源失败/旧缓存：口径或单位与 market_ff 不一致，拒绝写库
            self.stderr.write(
                f'大盘资金流历史来源为 {source!r}，非东财主源 {flows.MARKET_FUND_SOURCE_EM}，'
                f'拒绝写入快照（{hist.get("message") or "上游不可用"}）；东财恢复后重跑'
            )
            return

        latest = trading_calendar.expected_latest_date()
        # 日历不可用时保守只写到昨天，避免把盘中未收盘数值落库
        cutoff = latest or (timezone.localdate() - timedelta(days=1))
        written = skipped_existing = skipped_invalid = 0
        for item in hist.get('items', []):
            try:
                day = pd.to_datetime(item.get('date')).date()
            except Exception:
                skipped_invalid += 1
                continue
            if not isinstance(day, date_cls) or day > cutoff or item.get('main_net') is None:
                skipped_invalid += 1
                continue
            payload = [{
                'date': day.isoformat(),
                **{k: item.get(k) for k in snapshots.MARKET_FF_FLOW_KEYS},
                'src': flows.MARKET_FUND_SOURCE_EM,
                'unit': snapshots.MARKET_FF_UNIT,
                'scope': snapshots.MARKET_FF_SCOPE,
            }]
            with transaction.atomic():
                if force:
                    MarketDailySnapshot.objects.update_or_create(
                        kind=MarketDailySnapshot.KIND_MARKET_FF, trade_date=day,
                        defaults={'payload': payload},
                    )
                    written += 1
                    continue
                _, created = MarketDailySnapshot.objects.get_or_create(
                    kind=MarketDailySnapshot.KIND_MARKET_FF, trade_date=day,
                    defaults={'payload': payload},
                )
                if created:
                    written += 1
                else:
                    skipped_existing += 1
        self.stdout.write(self.style.SUCCESS(
            f'大盘资金流历史回补: 写入 {written} 天；已有跳过 {skipped_existing} 天；'
            f'无效/未收盘跳过 {skipped_invalid} 天' + ('（--force 覆盖模式）' if force else '')
        ))

    def _audit(self, delete=False):
        suspects = snapshots.audit_market_ff_snapshots()
        total = MarketDailySnapshot.objects.filter(kind=MarketDailySnapshot.KIND_MARKET_FF).count()
        self.stdout.write(f'market_ff 快照共 {total} 行，疑似异常 {len(suspects)} 行')
        for s in suspects:
            self.stdout.write(
                f"  {s['trade_date']} id={s['id']} main_net={s['main_net']} "
                f"src={s['src']} unit={s['unit']} reasons={','.join(s['reasons'])}"
            )
        if not delete:
            if suspects:
                self.stdout.write('只读模式：未改库。确认后可加 --delete-suspect 删除错口径行，再重跑回补')
            return
        ids = [s['id'] for s in suspects if _DELETABLE_REASONS & set(s['reasons'])]
        with transaction.atomic():
            deleted, _ = MarketDailySnapshot.objects.filter(
                kind=MarketDailySnapshot.KIND_MARKET_FF, pk__in=ids,
            ).delete()
        self.stdout.write(self.style.WARNING(f'已删除 {deleted} 行疑似污染快照（parts_all_none 仅提示未删）'))
