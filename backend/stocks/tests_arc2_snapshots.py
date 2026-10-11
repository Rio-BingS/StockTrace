"""ARC2 views-errors 重构的快照测试：锁定重构前的对外 JSON 输出（ARC-01/ARC-04）。

快照存放在 testdata/arc2_snapshots.json，比对的是渲染后的响应字节或
json.dumps 后的字符串（键顺序敏感），保证重构后逐字段、逐字节相同。
确需更新快照时（对外输出有意变化）：
    STOCKTRACE_UPDATE_SNAPSHOTS=1 python manage.py test stocks.tests_arc2_snapshots
并在提交说明里写清变化原因。
"""

import json
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from .models import AlertEvent, DailyQuote, PriceAlert, Stock, StockGroup

_SNAPSHOT_FILE = Path(__file__).resolve().parent / 'testdata' / 'arc2_snapshots.json'
_UPDATE = os.environ.get('STOCKTRACE_UPDATE_SNAPSHOTS') == '1'
_FIXED_NOW = timezone.make_aware(datetime(2026, 9, 4, 15, 30, 12, 345678))
_TODAY = date(2026, 9, 4)


def _load():
    if _SNAPSHOT_FILE.exists():
        return json.loads(_SNAPSHOT_FILE.read_text(encoding='utf-8'))
    return {}


class SnapshotMixin:
    def assertSnapshot(self, key, text):
        data = _load()
        if _UPDATE:
            data[key] = text
            _SNAPSHOT_FILE.parent.mkdir(exist_ok=True)
            _SNAPSHOT_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + '\n', encoding='utf-8',
            )
            return
        self.assertIn(key, data, f'缺快照 {key}，先用 STOCKTRACE_UPDATE_SNAPSHOTS=1 生成')
        self.assertEqual(text, data[key])


def _dump(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)


# ------------------------------------------------------------
# 行序列化：dashboard / 提醒 / 分组（步骤 3b）
# ------------------------------------------------------------

class RowSnapshotTests(SnapshotMixin, TestCase):
    def setUp(self):
        with mock.patch('django.utils.timezone.now', return_value=_FIXED_NOW):
            self.group = StockGroup.objects.create(name='银行', order=2)
            self.s1 = Stock.objects.create(
                code='600000', name='浦发银行', group=self.group,
                cost_price=Decimal('10.125'), quantity=1000,
            )
            self.s2 = Stock.objects.create(code='000001', name='')
            Stock.objects.create(code='300750', name='宁德时代', is_active=False)
            DailyQuote.objects.create(
                stock=self.s1, trade_date=date(2026, 9, 3),
                open_price=Decimal('10.000'), close_price=Decimal('10.500'),
                high_price=Decimal('10.800'), low_price=Decimal('9.900'),
                high_time=timezone.make_aware(datetime(2026, 9, 3, 10, 31)),
                low_time=timezone.make_aware(datetime(2026, 9, 3, 14, 2, 0, 500000)),
                prev_close=Decimal('10.100'), volume=123456789, turnover=Decimal('1234567.89'),
                open_close_diff=Decimal('0.500'), open_close_pct=Decimal('5.0000'),
                high_low_diff=Decimal('0.900'), high_low_pct=Decimal('9.0909'),
                change_diff=Decimal('0.400'), change_pct=Decimal('3.9604'),
            )
            DailyQuote.objects.create(
                stock=self.s1, trade_date=date(2026, 9, 4),
                open_price=Decimal('10.500'), close_price=Decimal('10.250'),
                high_price=Decimal('10.600'), low_price=Decimal('10.200'),
                prev_close=Decimal('10.500'),
                open_close_diff=Decimal('-0.250'), open_close_pct=Decimal('-2.3810'),
                high_low_diff=Decimal('0.400'), high_low_pct=Decimal('3.9216'),
            )
            self.alert = PriceAlert.objects.create(
                stock=self.s1, rule_type=PriceAlert.PRICE_ABOVE,
                threshold=Decimal('11.2500'), note='突破',
            )
            PriceAlert.objects.create(
                stock=self.s2, rule_type=PriceAlert.DAILY_PCT_BELOW,
                threshold=Decimal('3.0000'), is_active=False,
            )
            AlertEvent.objects.create(
                alert=self.alert, stock=self.s1, message='m', trade_date=date(2026, 9, 3),
            )
        PriceAlert.objects.filter(id=self.alert.id).update(
            last_triggered_at=timezone.make_aware(datetime(2026, 9, 3, 15, 0, 1, 123456)),
        )

    def test_dashboard(self):
        r = self.client.get('/api/dashboard/')
        self.assertEqual(r.status_code, 200)
        self.assertSnapshot('dashboard', r.content.decode())

    def test_alert_list(self):
        r = self.client.get('/api/alerts/')
        self.assertSnapshot('alert_list', r.content.decode())

    def test_alert_create_and_patch(self):
        with mock.patch('django.utils.timezone.now', return_value=_FIXED_NOW):
            r = self.client.post('/api/alerts/', {
                'stock_id': self.s1.id, 'rule_type': PriceAlert.DAILY_PCT_ABOVE,
                'threshold': '-2.123456', 'note': ' 急涨 ',
            }, content_type='application/json')
        self.assertEqual(r.status_code, 201)
        body = r.content.decode().replace(f'"id":{r.json()["id"]},', '"id":"<id>",')
        self.assertSnapshot('alert_create', body.replace(f'"stock_id":{self.s1.id},', '"stock_id":"<sid>",'))
        r = self.client.patch(f'/api/alerts/{self.alert.id}/', {'is_active': 'false', 'note': 'x'},
                              content_type='application/json')
        self.assertEqual(r.status_code, 200)
        body = r.content.decode().replace(f'"id":{self.alert.id},', '"id":"<id>",')
        self.assertSnapshot('alert_patch', body.replace(f'"stock_id":{self.s1.id},', '"stock_id":"<sid>",'))

    def test_group_list_create_patch(self):
        r = self.client.get('/api/stock-groups/')
        self.assertSnapshot('group_list', r.content.decode().replace(f'"id":{self.group.id},', '"id":"<id>",'))
        with mock.patch('django.utils.timezone.now', return_value=_FIXED_NOW):
            r = self.client.post('/api/stock-groups/', {'name': '新能源'}, content_type='application/json')
        self.assertEqual(r.status_code, 201)
        self.assertSnapshot('group_create', r.content.decode().replace(f'"id":{r.json()["id"]},', '"id":"<id>",'))
        r = self.client.patch(f'/api/stock-groups/{self.group.id}/', {'order': '5'},
                              content_type='application/json')
        self.assertSnapshot('group_patch', r.content.decode().replace(f'"id":{self.group.id},', '"id":"<id>",'))


# ------------------------------------------------------------
# 已是 {items, pagination, meta} 形状的接口：板块、ETF 雷达（步骤 4a）
# ------------------------------------------------------------

_FIXED_META = {'cache_status': 'fresh', 'source': 'stub'}


def _meta_stub(*args, **kwargs):
    return {**_FIXED_META, 'args': [str(a) for a in args], 'kwargs': {k: str(v) for k, v in kwargs.items()}}


def _sector_rows():
    return [
        {'name': f'行业{i}', 'net': float(n), 'inflow': 10.0 + i, 'outflow': 5.0, 'change_pct': c,
         'index_value': 1000.0 + i, 'company_count': 10 + i, 'leader': f'龙头{i}', 'leader_pct': 2.5}
        for i, (n, c) in enumerate([(5, 1.2), (-3, 0.5), (8, -0.4), (0, 0.0), (-9, -2.1), (2, None)])
    ]


class EnvelopeSnapshotTests(SnapshotMixin, SimpleTestCase):
    def setUp(self):
        for target in ('stocks.market.sectors._now_str', 'stocks.market.etf._now_str'):
            p = mock.patch(target, return_value='2026-09-04 15:30:00')
            p.start()
            self.addCleanup(p.stop)
        for target in ('stocks.market.sectors._cache_meta', 'stocks.market.etf._cache_meta'):
            p = mock.patch(target, side_effect=_meta_stub)
            p.start()
            self.addCleanup(p.stop)

    def test_sector_rotation_day(self):
        from .market import sectors

        with mock.patch.object(sectors, 'fetch_industry_fund_flow', return_value=_sector_rows()):
            data = sectors.get_sector_rotation(board='industry', period='day', sort='net', page=2, page_size=4)
        self.assertSnapshot('sectors_day', _dump(data))

    def test_sector_rotation_snapshot_periods(self):
        from .market import sectors, snapshots

        with mock.patch.object(snapshots, 'sector_multiday_nets', return_value=({'A': 3.0, 'B': -1.0}, 5)), \
                mock.patch.object(snapshots, '_latest_snapshot', return_value=None), \
                mock.patch.object(snapshots, 'snapshot_meta', side_effect=_meta_stub):
            data = sectors.get_sector_rotation(board='concept', period='5d')
        self.assertSnapshot('sectors_5d', _dump(data))
        with mock.patch.object(snapshots, 'sector_multiday_nets', return_value=(None, 2)), \
                mock.patch.object(snapshots, '_latest_snapshot', side_effect=RuntimeError('x')), \
                mock.patch.object(snapshots, 'snapshot_meta', side_effect=_meta_stub):
            data = sectors.get_sector_rotation(board='industry', period='10d', q='x')
        self.assertSnapshot('sectors_10d_missing', _dump(data))

    def test_etf_radar(self):
        from .market import etf, snapshots

        items = [
            {'code': '510300', 'name': '沪深300ETF', 'scope_match': True, 'share': 100.0,
             'main_net': 5.0, 'turnover': 2e9, 'data_date': '2026-09-03'},
            {'code': '159915', 'name': '创业板ETF', 'scope_match': True, 'share': None,
             'main_net': -2.0, 'turnover': 5e8, 'data_date': '2026-09-04'},
            {'code': '511010', 'name': '国债ETF', 'scope_match': False, 'share': 30.0,
             'main_net': None, 'turnover': None, 'data_date': None},
        ]

        def share_map(n):
            return ({'510300': 1.5} if n == 1 else None), None

        with mock.patch.object(etf, '_normalized_etf_items', return_value=[dict(i) for i in items]), \
                mock.patch.object(snapshots, 'share_change_map', side_effect=share_map):
            data = etf.get_etf_share_radar(scope='all', rank='share', page=1, page_size=2)
            self.assertSnapshot('etf_radar_all', _dump(data))
        with mock.patch.object(etf, '_normalized_etf_items', return_value=[dict(i) for i in items]), \
                mock.patch.object(snapshots, 'share_change_map', side_effect=share_map):
            data = etf.get_etf_share_radar(rank='turnover', sort='main_net', order='asc', q='etf',
                                           min_turnover=1e8)
            self.assertSnapshot('etf_radar_scoped', _dump(data))


# ------------------------------------------------------------
# 区间参数：全市场走势、国家队资金流（步骤 4b）
# ------------------------------------------------------------

_TREND_QUERIES = [
    '', 'period=1w', 'period=1m', 'period=3m', 'period=6m', 'period=1y', 'period=ytd',
    'days=30', 'days=60', 'days=120', 'days=250', 'days=',
    'start=2026-01-05&end=2026-06-30', 'start=2026-03-01', 'end=2026-06-30',
    'period=custom&start=2026-01-05&end=2026-06-30', 'period=3m&start=2026-08-01&end=2026-09-01',
    'start=2023-09-10&end=2026-09-04',
    # 非法输入：状态码与 400 文案均须保持
    'period=bad', 'days=45', 'days=abc', 'days=0', 'start=2026-08-10&end=2026-08-01',
    'start=2026/08/01', 'start=2022-01-01&end=2026-09-04',
]

_FLOW_QUERIES = [
    '', 'period=1d', 'period=3d', 'period=5d', 'period=1w', 'period=1m', 'period=3m', 'period=6m',
    'period=ytd', 'start=2026-08-20&end=2026-09-02', 'start=2026-08-25', 'end=2026-08-30',
    'start=2025-08-10&end=2026-09-04', 'period=custom&start=2026-08-20&end=2026-09-02',
    # 非法输入
    'period=bad', 'start=2026-08-10&end=2026-08-01', 'start=2026/08/01', 'end=2026-09-05',
    'start=2025-07-01&end=2026-09-04', 'start=2025-07-01',
]


def _flow_rows():
    from datetime import timedelta

    rows = []
    for i in range(120, -1, -1):
        d = _TODAY - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        rows.append({'date': d.isoformat(), 'main_net': float((i % 7) - 3), 'small_net': 0.0,
                     'mid_net': 0.0, 'large_net': 0.0, 'super_net': 0.0,
                     'close': 1.0 + i / 100, 'change_pct': 0.1})
    return rows


class PeriodSnapshotTests(SnapshotMixin, SimpleTestCase):
    def setUp(self):
        p = mock.patch('django.utils.timezone.localdate', return_value=_TODAY)
        p.start()
        self.addCleanup(p.stop)

    def test_market_trend_queries(self):
        out = []
        with mock.patch('stocks.views.market.get_market_trend', return_value={'series': []}) as trend:
            for q in _TREND_QUERIES:
                trend.reset_mock()
                r = self.client.get(f'/api/market/trend/?{q}')
                call = trend.call_args.kwargs if trend.call_args else None
                detail = r.json().get('detail') if r.status_code != 200 else None
                out.append([q, r.status_code, call, detail])
        self.assertSnapshot('trend_queries', _dump(out))

    def test_national_flow_queries(self):
        from .market import etf_flow

        rows = _flow_rows()
        out = []
        with mock.patch.object(etf_flow, 'flow_history', return_value=rows), \
                mock.patch.object(etf_flow, '_cache_get', return_value=None), \
                mock.patch.object(etf_flow, '_cache_set'), \
                mock.patch.object(etf_flow, '_cache_meta', side_effect=_meta_stub), \
                mock.patch.object(etf_flow.time, 'sleep'):
            for q in _FLOW_QUERIES:
                r = self.client.get(f'/api/market/national-etf/flow/?{q}')
                out.append([q, r.status_code, r.content.decode()])
        self.assertSnapshot('flow_queries', _dump(out))


class QuoteRowEquivalenceTests(TestCase):
    """QuoteRowSerializer 与旧 DashboardStockSerializer(_quote_row()) 输出一致（含高低点时间）。"""

    def test_same_as_legacy_dict_path(self):
        from .serializers import DashboardStockSerializer, QuoteRowSerializer
        from .views import _quote_row

        group = StockGroup.objects.create(name='g')
        stock = Stock.objects.create(code='600519', name='茅台', group=group, cost_price=Decimal('1500.1'))
        quote = DailyQuote.objects.create(
            stock=stock, trade_date=date(2026, 9, 4),
            open_price=Decimal('1.1'), close_price=Decimal('1.2'), high_price=Decimal('1.3'),
            low_price=Decimal('1.0'), high_time=_FIXED_NOW, low_time=_FIXED_NOW,
            open_close_diff=Decimal('0.1'), open_close_pct=Decimal('9.0909'),
            high_low_diff=Decimal('0.3'), high_low_pct=Decimal('30'),
            volume=7, turnover=Decimal('8.5'),
        )
        for q in (quote, None):
            with self.subTest(quote=q):
                stock.latest_quote = q
                self.assertEqual(
                    json.dumps(QuoteRowSerializer(stock).data),
                    json.dumps(DashboardStockSerializer(_quote_row(stock, q)).data),
                )
