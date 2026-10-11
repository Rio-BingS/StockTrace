"""探测器取数层（data-align）回归测试；各用例类注明对应条目编号。"""

from datetime import date, datetime, time as dt_time, timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from .models import MarketDailySnapshot

_TODAY = timezone.localdate()


def _weekday(day=None):
    """测试日历：周一至周五为交易日。"""
    return day.weekday() < 5


class MarginSnapshotRowTests(TestCase):
    """#9：两融快照行的融资余额要两市齐全才合计。"""

    def test_snapshot_row_rz_requires_both_markets(self):
        from .market.snapshots import margin_snapshot_row

        row = margin_snapshot_row(_TODAY, {'total': 10.0, 'rz': 9.8}, {'total': 8.0, 'rz': None})
        self.assertIsNone(row['rz'])
        row = margin_snapshot_row(_TODAY, {'total': 10.0, 'rz': 9.8}, {'total': 8.0, 'rz': 7.9})
        self.assertEqual(row['rz'], 17.7)
        self.assertEqual(row['total'], 18.0)


class BackfillMarginCommandTests(TestCase):
    """#9：backfill_margin 沪市批量 + 深市按日，失败可续跑。"""

    START, END = date(2026, 9, 28), date(2026, 10, 2)  # 周一至周五

    def _sh(self, start=None, end=None):
        out, day = {}, start
        while day <= end:
            out[day.strftime('%Y%m%d')] = {'total': 100.0, 'rz': 99.0, 'rq': 1.0}
            day += timedelta(days=1)
        return out

    def _run(self, sz_side_effect):
        out = StringIO()
        with patch('stocks.trading_calendar.is_trading_day', _weekday), \
                patch('stocks.market.sentiment._sh_margin_rows', side_effect=self._sh), \
                patch('stocks.market.sentiment._sz_margin_row', side_effect=sz_side_effect):
            call_command('backfill_margin', '--start', self.START.isoformat(),
                         '--end', self.END.isoformat(), '--sleep', '0', stdout=out)
        return out.getvalue()

    def _count(self):
        return MarketDailySnapshot.objects.filter(kind=MarketDailySnapshot.KIND_MARGIN).count()

    def test_partial_failure_then_resume(self):
        def flaky(day):
            return None if day == date(2026, 9, 30) else {'total': 80.0, 'rz': 79.0}

        with self.assertRaises(CommandError):
            self._run(flaky)
        self.assertEqual(self._count(), 4)
        row = MarketDailySnapshot.objects.get(kind=MarketDailySnapshot.KIND_MARGIN, trade_date=self.START)
        self.assertEqual(row.payload[0]['rz'], 178.0)

        probed = []

        def ok(day):
            probed.append(day)
            return {'total': 80.0, 'rz': 79.0}

        self._run(ok)
        self.assertEqual(self._count(), 5)
        self.assertEqual(probed, [date(2026, 9, 30)])  # 续跑只补缺的那天

    def test_rejects_inverted_range(self):
        with self.assertRaises(CommandError):
            call_command('backfill_margin', '--start', '2026-10-02', '--end', '2026-09-28')


class DetectorSchedulerDependencyTests(TestCase):
    """#14：探测器只等行业 + 概念板块快照，不再等 market_ff。"""

    def _ready(self, kinds, hh=17, mm=15):
        from . import scheduler

        for kind in kinds:
            MarketDailySnapshot.objects.create(kind=kind, trade_date=_TODAY, payload=[])
        at = timezone.make_aware(datetime.combine(_TODAY, dt_time(hh, mm)))
        with patch('stocks.scheduler.timezone.localtime', return_value=at):
            return scheduler._detector_dependencies_ready(_TODAY)

    def test_ready_without_market_ff(self):
        self.assertEqual(self._ready(('industry_ff', 'concept_ff')), (True, 'ready'))

    def test_waits_for_concept_ff(self):
        with self.assertLogs('stocks.scheduler', level='WARNING'):
            self.assertEqual(self._ready(('industry_ff', 'market_ff')), (False, 'waiting'))
