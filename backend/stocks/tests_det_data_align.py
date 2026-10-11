"""探测器取数层（data-align）回归测试；各用例类注明对应条目编号。"""

from datetime import datetime, time as dt_time
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from .models import MarketDailySnapshot

_TODAY = timezone.localdate()


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
