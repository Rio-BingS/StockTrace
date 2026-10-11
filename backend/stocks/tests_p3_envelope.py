"""P3 响应外壳过渡（ARC-04 步骤 4c/4d）的测试。

LegacySnapshotTests 锁定不带 ?envelope 时各列表接口的旧输出（逐字节），
快照存放在 testdata/p3_envelope_snapshots.json。确需更新时（对外输出有意变化）：
    STOCKTRACE_UPDATE_SNAPSHOTS=1 python manage.py test stocks.tests_p3_envelope
并在提交说明里写清变化原因。
"""

import json
import os
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.test import TestCase
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
