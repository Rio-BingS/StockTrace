"""P3 响应外壳过渡（ARC-04 步骤 4c/4d）的测试。

LegacySnapshotTests 锁定不带 ?envelope 时各列表接口的旧输出（逐字节），
快照存放在 testdata/p3_envelope_snapshots.json。确需更新时（对外输出有意变化）：
    STOCKTRACE_UPDATE_SNAPSHOTS=1 python manage.py test stocks.tests_p3_envelope
并在提交说明里写清变化原因。
"""

import json
import os
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from .models import AlertEvent, DailyQuote, MinuteBar, PriceAlert, ScreenerPreset, Stock, StockGroup

_SNAPSHOT_FILE = Path(__file__).resolve().parent / 'testdata' / 'p3_envelope_snapshots.json'
_UPDATE = os.environ.get('STOCKTRACE_UPDATE_SNAPSHOTS') == '1'
_FIXED_NOW = timezone.make_aware(datetime(2026, 9, 4, 15, 30, 12, 345678))
_TODAY = date(2026, 9, 4)
_ID_RE = re.compile(r'"(id|stock_id|alert_id|group|group_id)":(\d+)')


def _load():
    if _SNAPSHOT_FILE.exists():
        return json.loads(_SNAPSHOT_FILE.read_text(encoding='utf-8'))
    return {}


def _norm(text):
    """把自增 id 换成占位符，快照不受建表顺序影响。"""
    return _ID_RE.sub(r'"\1":"<id>"', text)


class SnapshotMixin:
    def assertSnapshot(self, key, text):
        data = _load()
        text = _norm(text)
        if _UPDATE:
            data[key] = text
            _SNAPSHOT_FILE.parent.mkdir(exist_ok=True)
            _SNAPSHOT_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + '\n', encoding='utf-8',
            )
            return
        self.assertIn(key, data, f'缺快照 {key}，先用 STOCKTRACE_UPDATE_SNAPSHOTS=1 生成')
        self.assertEqual(text, data[key])


_SEARCH_HITS = [{'code': '600000', 'name': '浦发银行'}, {'code': '600036', 'name': '招商银行'}]


class ListFixtureMixin:
    """各列表接口共用的固定数据（时间全部钉死，保证快照稳定）。"""

    def setUp(self):
        for target, value in (
            ('django.utils.timezone.now', _FIXED_NOW),
            ('django.utils.timezone.localdate', _TODAY),
        ):
            p = mock.patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        self.group = StockGroup.objects.create(name='银行', order=2)
        StockGroup.objects.create(name='空组', order=1)
        self.s1 = Stock.objects.create(
            code='600000', name='浦发银行', group=self.group,
            cost_price=Decimal('10.125'), quantity=1000,
        )
        self.s2 = Stock.objects.create(code='000001', name='')
        Stock.objects.create(code='300750', name='宁德时代', is_active=False)
        for d, close in ((date(2026, 9, 3), '10.500'), (date(2026, 9, 4), '10.250'), (date(2025, 1, 2), '8.000')):
            DailyQuote.objects.create(
                stock=self.s1, trade_date=d,
                open_price=Decimal('10.000'), close_price=Decimal(close),
                high_price=Decimal('10.800'), low_price=Decimal('7.900'),
                high_time=timezone.make_aware(datetime(d.year, d.month, d.day, 10, 31)),
                prev_close=Decimal('10.100'), volume=123456789, turnover=Decimal('1234567.89'),
                open_close_diff=Decimal('0.500'), open_close_pct=Decimal('5.0000'),
                high_low_diff=Decimal('2.900'), high_low_pct=Decimal('36.7089'),
            )
        for d, hh, mm in ((date(2026, 9, 3), 14, 59), (date(2026, 9, 4), 9, 31), (date(2026, 9, 4), 9, 32)):
            MinuteBar.objects.create(
                stock=self.s1, datetime=timezone.make_aware(datetime(d.year, d.month, d.day, hh, mm)),
                open=Decimal('10.010'), close=Decimal('10.020'), high=Decimal('10.030'),
                low=Decimal('10.000'), volume=1200, turnover=Decimal('12000.50'),
            )
        alert = PriceAlert.objects.create(stock=self.s1, rule_type=PriceAlert.PRICE_ABOVE, threshold=Decimal('11'))
        AlertEvent.objects.create(alert=alert, stock=self.s1, message='突破 11', trade_date=date(2026, 9, 3))
        AlertEvent.objects.create(alert=alert, stock=self.s1, message='再破 11', trade_date=date(2026, 9, 4),
                                  is_read=True)
        ScreenerPreset.objects.create(name='低估值', spec={'conditions': [{'field': 'pe', 'op': 'lt', 'value': 10}]})
        p = mock.patch('stocks.views.watchlist.search_stocks', return_value=_SEARCH_HITS)
        p.start()
        self.addCleanup(p.stop)


# 不带 envelope 时的旧输出：路径 → 快照键
LEGACY_CASES = (
    ('stocks_list', '/api/stocks/'),
    ('stocks_list_page1', '/api/stocks/?page=1'),
    ('stocks_search', '/api/stocks/search/?q=银行'),
    ('daily_default', '/api/stocks/{s1}/daily/'),
    ('daily_days', '/api/stocks/{s1}/daily/?days=30'),
    ('daily_date', '/api/stocks/{s1}/daily/?date=2026-09-03'),
    ('minutes_default', '/api/stocks/{s1}/minutes/'),
    ('minutes_date', '/api/stocks/{s1}/minutes/?date=2026-09-03'),
    ('minutes_empty', '/api/stocks/{s2}/minutes/'),
    ('dashboard', '/api/dashboard/'),
    ('groups', '/api/stock-groups/'),
    ('alert_events', '/api/alerts/events/'),
    ('alert_events_unread', '/api/alerts/events/?unread=1'),
    ('presets', '/api/screener/presets/'),
)


class LegacySnapshotTests(SnapshotMixin, ListFixtureMixin, TestCase):
    """不带 ?envelope（以及 envelope=0）时，输出与 v1.6.0 逐字节相同。"""

    def _get(self, path):
        r = self.client.get(path.format(s1=self.s1.id, s2=self.s2.id))
        self.assertEqual(r.status_code, 200, path)
        return r.content.decode()

    def test_legacy_outputs_unchanged(self):
        for key, path in LEGACY_CASES:
            with self.subTest(key=key):
                self.assertSnapshot(key, self._get(path))

    def test_envelope_off_values_keep_legacy(self):
        for key, path in LEGACY_CASES:
            sep = '&' if '?' in path else '?'
            for off in ('0', 'false', ''):
                with self.subTest(key=key, envelope=off):
                    self.assertSnapshot(key, self._get(f'{path}{sep}envelope={off}'))

    def test_dashboard_empty_is_bare_list(self):
        Stock.objects.update(is_active=False)
        self.assertEqual(self._get('/api/dashboard/'), '[]')


# ------------------------------------------------------------
# ?envelope=1：裸数组接口包成 {items, pagination, meta}（步骤 4c）
# ------------------------------------------------------------

# 裸数组接口：快照键（items 应与旧输出相同）、路径、期望 meta（None 表示只校验是对象）
ENVELOPE_CASES = (
    ('stocks_search', '/api/stocks/search/?q=银行', {'q': '银行'}),
    ('daily_default', '/api/stocks/{s1}/daily/', {'code': '600000', 'date': None, 'days': 365}),
    ('daily_days', '/api/stocks/{s1}/daily/?days=30', {'code': '600000', 'date': None, 'days': 30}),
    ('daily_date', '/api/stocks/{s1}/daily/?date=2026-09-03', {'code': '600000', 'date': '2026-09-03', 'days': None}),
    ('minutes_default', '/api/stocks/{s1}/minutes/', {'code': '600000', 'date': '2026-09-04'}),
    ('minutes_date', '/api/stocks/{s1}/minutes/?date=2026-09-03', {'code': '600000', 'date': '2026-09-03'}),
    ('minutes_empty', '/api/stocks/{s2}/minutes/', {'code': '000001', 'date': None}),
    ('dashboard', '/api/dashboard/', {}),
    ('groups', '/api/stock-groups/', {}),
    ('alert_events', '/api/alerts/events/', {'limit': 100, 'truncated': False, 'unread_only': False}),
    ('alert_events_unread', '/api/alerts/events/?unread=1', {'limit': 100, 'truncated': False, 'unread_only': True}),
    ('presets', '/api/screener/presets/', None),
)


class EnvelopeModeTests(ListFixtureMixin, TestCase):
    def _get(self, path, **params):
        r = self.client.get(path.format(s1=self.s1.id, s2=self.s2.id), params)
        self.assertEqual(r.status_code, 200, path)
        return r

    def test_bare_list_endpoints_wrap_items(self):
        snapshots = _load()
        for key, path, meta in ENVELOPE_CASES:
            for flag in ('1', 'true', 'YES'):
                with self.subTest(key=key, envelope=flag):
                    sep = '&' if '?' in path else '?'
                    body = self._get(f'{path}{sep}envelope={flag}').json()
                    self.assertEqual(list(body), ['items', 'pagination', 'meta'])
                    self.assertIsNone(body['pagination'])
                    self.assertIsInstance(body['meta'], dict)
                    if meta is not None:
                        self.assertEqual(body['meta'], meta)
                    # items 与旧裸数组逐项相同（键顺序也相同）
                    legacy = json.loads(snapshots[key])
                    self.assertEqual(json.loads(_norm(json.dumps(body['items'], separators=(',', ':'),
                                                                   ensure_ascii=False))), legacy)

    def test_presets_meta_counts_builtins(self):
        from .screener.strategies import BUILTIN_STRATEGIES

        body = self._get('/api/screener/presets/', envelope=1).json()
        self.assertEqual(body['meta'], {'builtin_count': len(BUILTIN_STRATEGIES)})
        self.assertEqual(len(body['items']), len(BUILTIN_STRATEGIES) + 1)

    def test_dashboard_empty(self):
        Stock.objects.update(is_active=False)
        self.assertEqual(self._get('/api/dashboard/', envelope=1).json(),
                         {'items': [], 'pagination': None, 'meta': {}})

    def test_alert_events_truncated_flag(self):
        alert = PriceAlert.objects.first()
        AlertEvent.objects.bulk_create([
            AlertEvent(alert=alert, stock=self.s1, message=f'e{i}', trade_date=date(2026, 1, 1) + timedelta(days=i))
            for i in range(99)
        ])
        body = self._get('/api/alerts/events/', envelope=1).json()
        self.assertEqual(len(body['items']), 100)
        self.assertTrue(body['meta']['truncated'])
        # 旧输出仍然最多 100 条
        self.assertEqual(len(self._get('/api/alerts/events/').json()), 100)
        AlertEvent.objects.filter(message='e0').delete()
        self.assertFalse(self._get('/api/alerts/events/', envelope=1).json()['meta']['truncated'])

    def test_bad_params_still_400_in_envelope_mode(self):
        r = self.client.get(f'/api/stocks/{self.s1.id}/daily/', {'envelope': 1, 'date': '2026/09/03'})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json(), {'detail': 'date 格式应为 YYYY-MM-DD'})

    def test_unknown_values_fall_back_to_legacy(self):
        for flag in ('2', 'on', 'True1'):
            with self.subTest(envelope=flag):
                self.assertIsInstance(self._get('/api/stock-groups/', envelope=flag).json(), list)


@override_settings(STOCKTRACE_ENVELOPE_DEFAULT=True)
class EnvelopeDefaultFlipTests(ListFixtureMixin, TestCase):
    """下一版翻转默认值的演练：不带参数即外壳，?envelope=0 仍拿到旧输出。"""

    def test_flip_default(self):
        body = self.client.get('/api/stock-groups/').json()
        self.assertEqual(list(body), ['items', 'pagination', 'meta'])
        self.assertIsInstance(self.client.get('/api/stock-groups/?envelope=0').json(), list)
        self.assertIsInstance(self.client.get('/api/dashboard/?envelope=false').json(), list)


class WantsEnvelopeTests(SimpleTestCase):
    def _req(self, query=''):
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        return Request(APIRequestFactory().get(f'/x/{query}'))

    def test_values(self):
        from .common.envelope import wants_envelope

        cases = {'': False, '?envelope=': False, '?envelope=1': True, '?envelope=true': True,
                 '?envelope=Yes': True, '?envelope=0': False, '?envelope=no': False, '?envelope=x': False}
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertIs(wants_envelope(self._req(query)), expected)
        with override_settings(STOCKTRACE_ENVELOPE_DEFAULT=True):
            self.assertTrue(wants_envelope(self._req()))
            self.assertTrue(wants_envelope(self._req('?envelope=')))
            self.assertFalse(wants_envelope(self._req('?envelope=0')))


# ------------------------------------------------------------
# 自选列表 ?envelope=1：EnvelopePagination（步骤 4d）
# ------------------------------------------------------------

class StockListEnvelopeTests(ListFixtureMixin, TestCase):
    def _get(self, **params):
        r = self.client.get('/api/stocks/', {'envelope': 1, **params})
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_default_page_contains_all_items(self):
        body = self._get()
        self.assertEqual(list(body), ['items', 'pagination', 'meta'])
        self.assertEqual(body['pagination'], {'page': 1, 'page_size': 100, 'total': 2, 'total_pages': 1})
        self.assertEqual(body['meta'], {})
        legacy = self.client.get('/api/stocks/').json()
        self.assertEqual(body['items'], legacy['results'])

    def test_page_size_and_clamping(self):
        Stock.objects.create(code='600036', name='招商银行')
        body = self._get(page_size=2, page=2)
        self.assertEqual([s['code'] for s in body['items']], ['600036'])
        self.assertEqual(body['pagination'], {'page': 2, 'page_size': 2, 'total': 3, 'total_pages': 2})
        # 超出总页数钳到最后一页、非法页码回到第 1 页（DRF 默认是 404）
        self.assertEqual(self._get(page_size=2, page=99)['pagination']['page'], 2)
        self.assertEqual(self._get(page_size=2, page='abc')['pagination']['page'], 1)
        # page_size 上限 1000，非法值回到默认 100
        self.assertEqual(self._get(page_size=5000)['pagination']['page_size'], 1000)
        self.assertEqual(self._get(page_size=0)['pagination']['page_size'], 100)
        self.assertEqual(self._get(page_size='x')['pagination']['page_size'], 100)

    def test_more_than_legacy_page_size(self):
        """旧分页每页 100 条会截断；外壳模式可用 page_size 一次取全。"""
        Stock.objects.bulk_create([Stock(code=f'{688000 + i}', name=f'S{i}') for i in range(120)])
        legacy = self.client.get('/api/stocks/').json()
        self.assertEqual((legacy['count'], len(legacy['results'])), (122, 100))
        body = self._get(page_size=1000)
        self.assertEqual(len(body['items']), 122)
        self.assertEqual(body['pagination']['total_pages'], 1)
        self.assertEqual(self._get()['pagination'], {'page': 1, 'page_size': 100, 'total': 122, 'total_pages': 2})

    def test_empty_list(self):
        Stock.objects.update(is_active=False)
        self.assertEqual(self._get(), {
            'items': [], 'pagination': {'page': 1, 'page_size': 100, 'total': 0, 'total_pages': 1}, 'meta': {},
        })

    def test_detail_and_actions_unaffected(self):
        r = self.client.get(f'/api/stocks/{self.s1.id}/', {'envelope': 1})
        self.assertEqual(r.json()['code'], '600000')
        r = self.client.patch(f'/api/stocks/{self.s1.id}/?envelope=1', {'quantity': 200},
                              content_type='application/json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['quantity'], 200)


# ------------------------------------------------------------
# 探测器信号列表改用 envelope() 组装：键顺序与字节不变（步骤 4a 收尾）
# ------------------------------------------------------------

def _trading_true(_d):
    return True


@mock.patch('stocks.trading_calendar.is_trading_day', _trading_true)
class DetectorSignalsEnvelopeTests(SnapshotMixin, TestCase):
    def setUp(self):
        from .models import DetectorRun, DetectorSignal

        for target, value in (
            ('django.utils.timezone.now', _FIXED_NOW),
            ('django.utils.timezone.localdate', _TODAY),
        ):
            p = mock.patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        DetectorRun.objects.create(trade_date=_TODAY, status='ok', targets_scanned=10, calm_count=5, matrix={})
        for code, sev, score in (('半导体', DetectorSignal.SEVERITY_HIGH, 55), ('银行', DetectorSignal.SEVERITY_LOW, 20)):
            DetectorSignal.objects.create(
                target_kind='industry', target_code=code, target_name=code,
                trade_date=_TODAY, direction='up', cycles=2, score=score,
                severity=sev, status=DetectorSignal.STATUS_ACTIVE,
                dimensions={'vol_compress': {'name': '波动率压缩', 'value': 42, 'direction': 'up',
                                             'weight': 1.0, 'available': True, 'note': ''}},
            )

    def test_signals_output_and_key_order(self):
        for key, params in (('detector_signals', {}), ('detector_signals_page2', {'page': 2, 'page_size': 1}),
                            ('detector_signals_empty', {'date': '2020-01-01'})):
            with self.subTest(key=key):
                r = self.client.get('/api/detector/signals/', params)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(list(r.json()),
                                 ['meta', 'trade_date', 'latest_run', 'summary', 'pagination', 'items'])
                self.assertSnapshot(key, r.content.decode())
