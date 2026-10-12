"""限售解禁日程 StockRestrictedRelease：东财 RPT_LIFT_STAGE 解析、合并与窗口覆盖（det-v2）。

沙箱测试断网（IsolatedTestRunner），上游返回用 fixture 模拟；真实接口须在 VPS 验证。
"""

from datetime import date
from unittest.mock import patch

from django.test import TestCase

from .market import restricted_release as rr
from .models import StockRestrictedRelease

_ROW = {
    'SECURITY_CODE': '301077', 'SECURITY_NAME_ABBR': '星华新材', 'FREE_DATE': '2025-04-01 00:00:00',
    'FREE_SHARES_TYPE': '首发原股东限售股份', 'CURRENT_FREE_SHARES': 2603.2499,
    'ABLE_FREE_SHARES': 7712.9998, 'LIFT_MARKET_CAP': 72188.119727, 'FREE_RATIO': 0.0399, 'BATCH_HOLDER_NUM': 3,
}


def _page(rows, pages=1):
    return {'result': {'pages': pages, 'data': rows}, 'success': True}


class ParseRowsTests(TestCase):
    def test_units_and_merge_same_key(self):
        twin = dict(_ROW, CURRENT_FREE_SHARES=100, FREE_RATIO=0.01, LIFT_MARKET_CAP=10, BATCH_HOLDER_NUM=1)
        out = rr.parse_rows([_ROW, twin, dict(_ROW, SECURITY_CODE='BJ1'), dict(_ROW, FREE_DATE=None)])
        self.assertEqual(len(out), 1)
        row = out[('301077', date(2025, 4, 1), '首发原股东限售股份')]
        # 万股 → 股、万元 → 元；同键批次相加
        self.assertAlmostEqual(row['free_shares'], (2603.2499 + 100) * 1e4)
        self.assertAlmostEqual(row['free_ratio'], 0.0499)
        self.assertAlmostEqual(row['lift_market_cap'], (72188.119727 + 10) * 1e4)
        self.assertEqual(row['holder_num'], 4)

    def test_different_types_same_day_are_separate_rows(self):
        out = rr.parse_rows([_ROW, dict(_ROW, FREE_SHARES_TYPE='股权激励限售股份')])
        self.assertEqual(len(out), 2)


class FetchReleasesTests(TestCase):
    def test_paginates_until_last_page(self):
        pages = [_page([_ROW], pages=2), _page([dict(_ROW, SECURITY_CODE='600000')], pages=2)]
        with patch.object(rr, '_get_page', side_effect=pages) as get:
            out = rr.fetch_releases(date(2025, 4, 1), date(2025, 4, 30))
        self.assertEqual(get.call_count, 2)
        self.assertEqual({k[0] for k in out}, {'301077', '600000'})

    def test_empty_range_returns_empty(self):
        with patch.object(rr, '_get_page', return_value={'result': None, 'code': 9201}):
            self.assertEqual(rr.fetch_releases(date(2030, 1, 1), date(2030, 1, 2)), {})

    def test_missing_later_page_raises(self):
        with patch.object(rr, '_get_page', side_effect=[_page([_ROW], pages=2), {'result': None}]):
            with self.assertRaises(rr.ReleaseFetchError):
                rr.fetch_releases(date(2025, 4, 1), date(2025, 4, 30))

    def test_network_error_raises_fetch_error(self):
        import requests

        with patch('stocks.market.restricted_release.requests.get',
                   side_effect=requests.ConnectionError('blocked')):
            with self.assertRaises(rr.ReleaseFetchError):
                rr.fetch_releases(date(2025, 4, 1), date(2025, 4, 30))


class RefreshWindowTests(TestCase):
    def test_window_is_replaced_and_outside_kept(self):
        today = date(2026, 10, 12)
        # 窗口内的旧排期（已推迟）应被删掉；窗口外的历史保留
        StockRestrictedRelease.objects.create(code='600000', free_date=date(2026, 11, 2), share_type='旧')
        StockRestrictedRelease.objects.create(code='600000', free_date=date(2026, 1, 5), share_type='历史')
        fresh = rr.parse_rows([dict(_ROW, FREE_DATE='2026-11-20 00:00:00')])
        with patch.object(rr, 'fetch_releases', return_value=fresh) as fetch:
            start, end, n = rr.refresh_restricted_releases(today=today)
        fetch.assert_called_once_with(date(2026, 10, 7), date(2026, 12, 11))
        self.assertEqual((start, end, n), (date(2026, 10, 7), date(2026, 12, 11), 1))
        self.assertEqual(sorted(StockRestrictedRelease.objects.values_list('share_type', flat=True)),
                         ['历史', '首发原股东限售股份'])
        self.assertEqual(StockRestrictedRelease.objects.get(code='301077').source, rr.SOURCE_TAG)

    def test_fetch_failure_keeps_old_rows(self):
        StockRestrictedRelease.objects.create(code='600000', free_date=date(2026, 11, 2), share_type='旧')
        with patch.object(rr, 'fetch_releases', side_effect=rr.ReleaseFetchError('x')):
            with self.assertRaises(rr.ReleaseFetchError):
                rr.refresh_restricted_releases(today=date(2026, 10, 12))
        self.assertEqual(StockRestrictedRelease.objects.count(), 1)


class RestrictedReleaseJobTests(TestCase):
    def test_job_registered_daily_1800_beijing(self):
        from zoneinfo import ZoneInfo

        from stocks import scheduler as sch

        job = {j.id: j for j in sch.build_scheduler().get_jobs()}['restricted_release']
        fields = {f.name: str(f) for f in job.trigger.fields}
        self.assertEqual((fields['hour'], fields['minute'], fields['day_of_week']), ('18', '0', '*'))
        self.assertEqual(job.trigger.timezone, ZoneInfo('Asia/Shanghai'))

    def test_job_swallows_fetch_error(self):
        from stocks import scheduler as sch

        with patch.object(rr, 'refresh_restricted_releases', side_effect=rr.ReleaseFetchError('x')) as ref:
            sch._restricted_release_job()
        ref.assert_called_once_with()


class BackfillRestrictedReleaseCommandTests(TestCase):
    def test_quarter_slices_cover_range_without_gaps(self):
        from .management.commands.backfill_restricted_release import quarter_slices

        self.assertEqual(quarter_slices(date(2025, 2, 10), date(2025, 10, 5)), [
            (date(2025, 2, 10), date(2025, 3, 31)),
            (date(2025, 4, 1), date(2025, 6, 30)),
            (date(2025, 7, 1), date(2025, 9, 30)),
            (date(2025, 10, 1), date(2025, 10, 5)),
        ])
        self.assertEqual(quarter_slices(date(2025, 12, 1), date(2026, 1, 2))[-1],
                         (date(2026, 1, 1), date(2026, 1, 2)))

    def test_failed_slice_reported_and_others_written(self):
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        def fake_fetch(a, b):
            if a.month == 4:
                raise rr.ReleaseFetchError('page 2')
            return rr.parse_rows([dict(_ROW, FREE_DATE=f'{a.isoformat()} 00:00:00')])

        with patch.object(rr, 'fetch_releases', side_effect=fake_fetch):
            with self.assertRaises(CommandError):
                call_command('backfill_restricted_release', '--start', '2025-01-01', '--end', '2025-09-30',
                             '--sleep', '0', stdout=StringIO(), stderr=StringIO())
        self.assertEqual(sorted(StockRestrictedRelease.objects.values_list('free_date', flat=True)),
                         [date(2025, 1, 1), date(2025, 7, 1)])


class StockEventsApiTests(TestCase):
    def setUp(self):
        p = patch('stocks.detector.views.expected_latest_date', return_value=date(2026, 10, 9))
        p.start()
        self.addCleanup(p.stop)
        p2 = patch('stocks.trading_calendar.is_trading_day', side_effect=lambda d: d.weekday() < 5)
        p2.start()
        self.addCleanup(p2.stop)

    def test_triggered_lift_and_upcoming_list(self):
        for d, ratio, kind in ((date(2026, 10, 9), 0.5, '当日'), (date(2026, 10, 20), 0.012, '首发原股东限售股份'),
                               (date(2026, 11, 30), 0.3, '定向增发机构配售股份')):
            StockRestrictedRelease.objects.create(code='600000', free_date=d, share_type=kind, free_ratio=ratio)
        body = self.client.get('/api/detector/stock-events/600000/').json()
        self.assertTrue(body['available'])
        self.assertTrue(body['lift']['triggered'])
        self.assertEqual(body['lift']['first_date'], '2026-10-20')
        self.assertEqual(body['lift']['days_to'], 7)
        # T 当日的解禁不再提示；60 日内的排期都列出
        self.assertEqual([u['date'] for u in body['upcoming']], ['2026-10-20', '2026-11-30'])

    def test_not_triggered_when_beyond_20_trading_days(self):
        StockRestrictedRelease.objects.create(code='600000', free_date=date(2026, 11, 30), share_type='x',
                                              free_ratio=0.3)
        body = self.client.get('/api/detector/stock-events/600000/').json()
        self.assertFalse(body['lift']['triggered'])
        self.assertEqual(len(body['upcoming']), 1)

    def test_schedule_not_loaded_and_bad_code(self):
        body = self.client.get('/api/detector/stock-events/600000/').json()
        self.assertFalse(body['available'])
        self.assertEqual(self.client.get('/api/detector/stock-events/abc/').status_code, 400)
