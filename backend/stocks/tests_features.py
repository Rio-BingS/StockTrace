"""新增功能测试：日度快照（板块 20 日轮动 / ETF 份额变化）、价格提醒、持仓成本、选股预设。

交易日历统一 patch 为「每天都交易日」，使窗口= 自然日窗口，保持确定性。
"""

import json

import pandas as pd
from datetime import date, datetime, time as dt_time, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from . import alerts, market
from .market import snapshots
from .models import (
    AlertEvent,
    DailyQuote,
    MarketDailySnapshot,
    MinuteBar,
    PriceAlert,
    Stock,
)
from .views import _quote_row

_BASE = date(2026, 9, 4)


def _trading_true(day=None):
    return True


def _no_holders(code, **kwargs):
    """持有人结构是独立上游（东财基金档案），ETF 详情用例不关心：固定为不可用，避免触网（OPS-03）"""
    return {'available': False, 'items': [], 'message': '测试桩：不拉持有人结构', 'meta': {}}


def _make_sector_snapshots(kind, days, names_by_day):
    """days: 相对 _BASE 往前的天数列表；names_by_day: offset -> {name: net}"""
    for offset, nets in names_by_day.items():
        MarketDailySnapshot.objects.create(
            kind=kind,
            trade_date=_BASE - timedelta(days=offset),
            payload=[{'name': k, 'net': v, 'change_pct': None} for k, v in nets.items()],
        )


@patch('stocks.trading_calendar.is_trading_day', _trading_true)
@patch('stocks.market.etf_flow.fetch_flow_klines', new=Mock(side_effect=RuntimeError('offline')))
class SnapshotSaveTests(TestCase):
    def test_save_writes_industry_and_etf_but_skips_empty_concept(self):
        with patch(
            'stocks.market.sectors.fetch_industry_fund_flow',
            return_value=[{'name': '半导体', 'net': 1e8, 'change_pct': 1.5}],
        ), patch(
            'stocks.market.sectors.fetch_concept_fund_flow',
            return_value=[],
        ), patch(
            'stocks.market.etf._normalized_etf_items',
            return_value=[{'code': '510300', 'name': '300ETF', 'share': 100.0, 'market_cap': 400.0}],
        ), patch('stocks.market.snapshots._etf_spot_fresh_for', return_value=True), \
                patch('akshare.fund_etf_scale_sse', return_value=pd.DataFrame()):
            saved = snapshots.save_daily_snapshots(trade_date=_BASE)

        self.assertEqual(saved[MarketDailySnapshot.KIND_INDUSTRY_FF], 1)
        self.assertEqual(saved[MarketDailySnapshot.KIND_ETF_SHARE], 1)
        # 无数据不写行，不产生空快照
        self.assertNotIn(MarketDailySnapshot.KIND_CONCEPT_FF, saved)
        self.assertFalse(
            MarketDailySnapshot.objects.filter(kind=MarketDailySnapshot.KIND_CONCEPT_FF).exists()
        )

    def test_save_is_idempotent_same_day(self):
        items = [{'name': '半导体', 'net': 1e8, 'change_pct': 1.5}]
        with patch(
            'stocks.market.sectors.fetch_industry_fund_flow', return_value=items,
        ), patch(
            'stocks.market.sectors.fetch_concept_fund_flow', return_value=[],
        ), patch(
            'stocks.market.etf._normalized_etf_items', return_value=[],
        ), patch('stocks.market.snapshots._etf_spot_fresh_for', return_value=True), patch('akshare.fund_etf_scale_sse', return_value=pd.DataFrame()):
            snapshots.save_daily_snapshots(trade_date=_BASE)
            items[0]['net'] = 2e8
            snapshots.save_daily_snapshots(trade_date=_BASE)

        row = MarketDailySnapshot.objects.get(
            kind=MarketDailySnapshot.KIND_INDUSTRY_FF, trade_date=_BASE,
        )
        self.assertEqual(row.payload[0]['net'], 2e8)  # 重跑覆盖而非新增


class SectorMultidayTests(TestCase):
    def _with_window(self):
        with patch('stocks.trading_calendar.is_trading_day', _trading_true), patch(
            'stocks.market.snapshots._expected_latest_date', return_value=_BASE,
        ):
            return snapshots.sector_multiday_nets(
                MarketDailySnapshot.KIND_INDUSTRY_FF, 3,
            )

    def test_incomplete_window_is_unavailable(self):
        _make_sector_snapshots(MarketDailySnapshot.KIND_INDUSTRY_FF, 3, {
            0: {'A': 100.0},
            1: {'A': 50.0},
            # 缺 offset=2 的那天
        })
        nets, covered = self._with_window()
        self.assertIsNone(nets)
        self.assertEqual(covered, 2)

    def test_sums_and_excludes_names_with_missing_days(self):
        _make_sector_snapshots(MarketDailySnapshot.KIND_INDUSTRY_FF, 3, {
            0: {'A': 25.0},
            1: {'A': 50.0, 'B': 10.0},
            2: {'A': 100.0, 'B': 20.0},
        })
        nets, covered = self._with_window()
        self.assertEqual(nets, {'A': 175.0})  # B 只有一天，窗口不完整被剔除
        self.assertEqual(covered, 3)


class SectorRotation20dTests(TestCase):
    def test_20d_without_snapshots_honestly_reports_accumulation(self):
        result = market.get_sector_rotation(board='industry', period='20d')
        self.assertFalse(result['available'])
        self.assertIn('快照', result['message'])
        self.assertEqual(result['supported_periods'], ['day', '5d', '10d', '20d'])

    def test_20d_with_full_window_sums_daily_nets(self):
        _make_sector_snapshots(MarketDailySnapshot.KIND_INDUSTRY_FF, 20, {
            offset: {'半导体': 10.0} for offset in range(20)
        })
        with patch('stocks.trading_calendar.is_trading_day', _trading_true), patch(
            'stocks.market.snapshots._expected_latest_date', return_value=_BASE,
        ):
            result = market.get_sector_rotation(
                board='industry', period='20d', sort='net', order='desc',
            )
        self.assertTrue(result['available'])
        self.assertEqual(result['items'][0]['net'], 200.0)
        self.assertTrue(result['meta']['available'])

    def test_20d_rejects_non_net_sort(self):
        with self.assertRaises(ValueError):
            market.get_sector_rotation(board='industry', period='20d', sort='change_pct')


class EtfShareChangeTests(TestCase):
    def _map(self, n):
        with patch('stocks.trading_calendar.is_trading_day', _trading_true), patch(
            'stocks.market.snapshots._expected_latest_date', return_value=_BASE,
        ):
            return snapshots.share_change_map(n)

    def test_share_change_vs_n_trading_days_ago(self):
        shares = {0: 110.0, 1: 105.0, 2: 103.0, 3: 102.0, 4: 101.0, 5: 100.0}
        for offset, share in shares.items():
            MarketDailySnapshot.objects.create(
                kind=MarketDailySnapshot.KIND_ETF_SHARE,
                trade_date=_BASE - timedelta(days=offset),
                payload=[{'code': '510300', 'name': '300ETF', 'share': share, 'market_cap': None}],
            )
        chg, _covered = self._map(1)
        self.assertEqual(chg['510300'], 5.0)  # vs 1 个交易日前
        chg5, _ = self._map(5)
        self.assertEqual(chg5, {'510300': 10.0})  # vs 5 个交易日前（最早一天）

    def test_insufficient_window_returns_none(self):
        MarketDailySnapshot.objects.create(
            kind=MarketDailySnapshot.KIND_ETF_SHARE,
            trade_date=_BASE,
            payload=[{'code': '510300', 'name': '300ETF', 'share': 100.0, 'market_cap': None}],
        )
        chg, covered = self._map(5)
        self.assertIsNone(chg)
        self.assertEqual(covered, 1)

    def test_radar_attaches_changes_when_window_complete(self):
        for offset, share in ((0, 110.0), (1, 100.0)):
            MarketDailySnapshot.objects.create(
                kind=MarketDailySnapshot.KIND_ETF_SHARE,
                trade_date=_BASE - timedelta(days=offset),
                payload=[{'code': '510300', 'name': '300ETF', 'share': share, 'market_cap': None}],
            )
        with patch(
            'stocks.market.etf._fetch_etf_spot_df',
            return_value=pd.DataFrame([
                {'代码': '510300', '名称': '300ETF', '最新份额': 110, '成交额': 1000},
            ]),
        ), patch('stocks.trading_calendar.is_trading_day', _trading_true), patch(
            'stocks.market.snapshots._expected_latest_date', return_value=_BASE,
        ):
            result = market.get_etf_share_radar()
        self.assertTrue(result['supported_metrics']['share_change_1d'])
        item = next(i for i in result['items'] if i['code'] == '510300')
        self.assertEqual(item['share_chg_1d'], 10.0)
        self.assertIsNone(item['share_chg_5d'])  # 窗口未凑齐如实为 null


class EtfShareSignalTests(TestCase):
    """宽基份额异动信号：最近两日逐 ETF 对比 + 同步异动判定。"""

    def setUp(self):
        market._cache.clear()

    def _two_days(self, rows_prev, rows_latest):
        for offset, rows in ((1, rows_prev), (0, rows_latest)):
            MarketDailySnapshot.objects.create(
                kind=MarketDailySnapshot.KIND_ETF_SHARE,
                trade_date=_BASE - timedelta(days=offset),
                payload=rows,
            )

    def _with_calendar(self):
        return patch('stocks.trading_calendar.is_trading_day', _trading_true), patch(
            'stocks.market.snapshots._expected_latest_date', return_value=_BASE,
        )

    def test_latest_pair_change_pct_and_skips_new_listing(self):
        self._two_days(
            [{'code': '510300', 'name': '沪深300ETF', 'share': 100e8, 'market_cap': 400e8}],
            [
                {'code': '510300', 'name': '沪深300ETF', 'share': 102e8, 'market_cap': 408e8, 'turnover': 5e9},
                {'code': '159949', 'name': '创业板50ETF', 'share': 90e8, 'market_cap': 300e8},
            ],
        )
        p1, p2 = self._with_calendar()
        with p1, p2:
            changes, meta = snapshots.latest_pair_change()
        self.assertEqual(meta, {
            'date': _BASE.isoformat(),
            'prev_date': (_BASE - timedelta(days=1)).isoformat(),
        })
        self.assertNotIn('159949', changes)  # 无基期（新上市/新增）跳过
        row = changes['510300']
        self.assertAlmostEqual(row['chg'], 2e8)
        self.assertAlmostEqual(row['chg_pct'], 2.0)
        self.assertEqual(row['turnover'], 5e9)

    def test_pair_change_degrades_without_two_snapshots(self):
        MarketDailySnapshot.objects.create(
            kind=MarketDailySnapshot.KIND_ETF_SHARE,
            trade_date=_BASE,
            payload=[{'code': '510300', 'name': '沪深300ETF', 'share': 100e8, 'market_cap': 400e8}],
        )
        p1, p2 = self._with_calendar()
        with p1, p2:
            changes, meta = snapshots.latest_pair_change()
        self.assertIsNone(changes)
        self.assertIsNone(meta)

    def test_sync_in_detected_and_noise_filtered(self):
        broad = ('510300', '510310', '510320')
        self._two_days(
            [
                {'code': c, 'name': '沪深300ETF', 'share': 100e8, 'market_cap': 400e8} for c in broad
            ] + [
                {'code': '510500', 'name': '500ETF', 'share': 50e8, 'market_cap': 250e8},
                {'code': '510330', 'name': '华夏300ETF', 'share': 20e8, 'market_cap': 80e8},
                {'code': '510050', 'name': '50ETF', 'share': 100e8, 'market_cap': 280e8},
                {'code': '511990', 'name': '货币ETF', 'share': 100e8, 'market_cap': 100e8},
            ],
            [
                {'code': c, 'name': '沪深300ETF', 'share': 102e8, 'market_cap': 408e8} for c in broad
            ] + [
                {'code': '510500', 'name': '500ETF', 'share': 50.2e8, 'market_cap': 250e8},   # +0.4%：低于百分比阈值
                {'code': '510330', 'name': '华夏300ETF', 'share': 20.4e8, 'market_cap': 80e8},  # +2% 但仅 0.4 亿份：低于绝对阈值
                {'code': '510050', 'name': '50ETF', 'share': 98e8, 'market_cap': 280e8},       # -2%/-2亿份：单只净赎回
                {'code': '511990', 'name': '货币ETF', 'share': 106e8, 'market_cap': 106e8},    # +6%：货币不在宽基范围
            ],
        )
        p1, p2 = self._with_calendar()
        with p1, p2:
            result = market.etf._share_change_signals()
        self.assertTrue(result['available'])
        self.assertEqual(result['signal'], 'sync_in')
        self.assertEqual(result['in_count'], 3)
        self.assertEqual(result['out_count'], 1)
        self.assertEqual({r['code'] for r in result['in_items']}, set(broad))
        self.assertEqual(result['out_items'][0]['code'], '510050')
        # 净申购金额估算：份额变化 × 净值（总市值/份额）；有市值的行有估算值
        in_row = result['in_items'][0]
        self.assertAlmostEqual(in_row['est_amount'], 2e8 * (408e8 / 102e8), delta=1)

    def test_below_sync_threshold_reports_none(self):
        self._two_days(
            [{'code': '510300', 'name': '沪深300ETF', 'share': 100e8, 'market_cap': 400e8}],
            [{'code': '510300', 'name': '沪深300ETF', 'share': 103e8, 'market_cap': 410e8}],
        )
        p1, p2 = self._with_calendar()
        with p1, p2:
            result = market.etf._share_change_signals()
        self.assertTrue(result['available'])
        self.assertEqual(result['signal'], 'none')
        self.assertEqual(result['in_count'], 1)

    def test_watchlist_response_carries_signals(self):
        self._two_days(
            [{'code': '510300', 'name': '沪深300ETF', 'share': 100e8, 'market_cap': 400e8}],
            [{'code': '510300', 'name': '沪深300ETF', 'share': 103e8, 'market_cap': 410e8}],
        )
        with patch(
            'stocks.market.etf._fetch_etf_spot_df',
            return_value=pd.DataFrame([
                {'代码': '510300', '名称': '沪深300ETF', '最新价': 4.2, '最新份额': 103e8, '数据日期': '2026-09-04'},
            ]),
        ), patch('stocks.trading_calendar.is_trading_day', _trading_true), patch(
            'stocks.market.snapshots._expected_latest_date', return_value=_BASE,
        ):
            data = market.get_national_team_etfs(force=True)
        self.assertTrue(data['signals']['available'])
        self.assertEqual(data['signals']['signal'], 'none')
        self.assertIn('启发式', data['signals']['message'])


class SnapshotTradingDayGuardTests(TestCase):
    """非交易日落库守卫：周末手动触发的快照会卡死窗口指标时效门槛。"""

    def test_non_trading_day_skips_snapshot(self):
        saved = snapshots.save_daily_snapshots(trade_date=_BASE + timedelta(days=1))  # 09-05 周六
        self.assertEqual(saved, {})
        self.assertFalse(
            MarketDailySnapshot.objects.filter(trade_date=_BASE + timedelta(days=1)).exists()
        )


class SseShareBackfillTests(TestCase):
    """上交所历史份额回填：解析、幂等落库与详情份额曲线。"""

    def setUp(self):
        market._cache.clear()

    def test_fetch_sse_share_rows_parses_and_degrades(self):
        import pandas as pd

        df = pd.DataFrame([
            {'基金代码': '510300', '基金简称': '300ETF', '基金份额': 233.77e8},
            {'基金代码': '510050', '基金简称': '50ETF', '基金份额': 0},  # 非正份额跳过
            {'基金代码': '', '基金简称': '坏行', '基金份额': 1.0},
        ])
        with patch('akshare.fund_etf_scale_sse', return_value=df):
            rows = snapshots.fetch_sse_share_rows(_BASE)
        self.assertEqual(rows, [
            {'code': '510300', 'name': '300ETF', 'share': 233.77e8, 'market_cap': None, 'turnover': None},
        ])
        with patch('akshare.fund_etf_scale_sse', side_effect=RuntimeError('boom')):
            self.assertEqual(snapshots.fetch_sse_share_rows(_BASE), [])
        with patch('akshare.fund_etf_scale_sse', return_value=None):
            self.assertEqual(snapshots.fetch_sse_share_rows(_BASE), [])

    def test_share_history_series_filters_code_and_sorts(self):
        for offset in (1, 0):
            MarketDailySnapshot.objects.create(
                kind=MarketDailySnapshot.KIND_ETF_SHARE,
                trade_date=_BASE - timedelta(days=offset),
                payload=[
                    {'code': '510300', 'name': '300ETF', 'share': 100.0 + offset, 'market_cap': None},
                    {'code': '159919', 'name': '300ETF深', 'share': 999.0, 'market_cap': None},
                ],
            )
        series = snapshots.share_history_series('510300', _BASE - timedelta(days=1), _BASE)
        self.assertEqual([i['share'] for i in series], [101.0, 100.0])  # 升序，仅该代码
        self.assertEqual(snapshots.share_history_series('999999', _BASE - timedelta(days=1), _BASE), [])

    @patch('stocks.trading_calendar.is_trading_day', _trading_true)
    @patch('stocks.market.etf.fetch_holder_structure', new=_no_holders)
    def test_detail_includes_share_history(self):
        for offset in (2, 1):
            MarketDailySnapshot.objects.create(
                kind=MarketDailySnapshot.KIND_ETF_SHARE,
                trade_date=date.today() - timedelta(days=offset + 1),
                payload=[{'code': '510300', 'name': '300ETF', 'share': 100.0 + offset, 'market_cap': None}],
            )
        with patch(
            'stocks.market.etf._normalized_etf_items',
            return_value=[{'code': '510300', 'name': '300ETF', 'exchange': 'SH', 'share': 102.0}],
        ), patch(
            'stocks.market.etf._etf_history',
            return_value=[
                {'date': '2026-09-04', 'close': 4.0},
                {'date': '2026-09-05', 'close': 4.1},
            ],
        ):
            data = market.get_etf_detail('510300', range_name='1m')
        self.assertTrue(data['share_history']['available'])
        self.assertEqual(len(data['share_history']['items']), 2)
        self.assertEqual(
            data['share_history']['items'][0]['date'],
            (date.today() - timedelta(days=3)).isoformat(),
        )


def _make_quote(stock, close, change_pct, day=_BASE):
    return DailyQuote.objects.create(
        stock=stock, trade_date=day,
        open_price=close, close_price=close, high_price=close, low_price=close,
        open_close_diff=Decimal('0'), open_close_pct=Decimal('0'),
        high_low_diff=Decimal('0'), high_low_pct=Decimal('0'),
        change_pct=Decimal(str(change_pct)),
    )


# P0 DAT-08 后提醒只认当日数据且盘前/午休不评估：测试数据落在「今天」，评估时刻固定为盘中
_ALERT_DAY = timezone.localdate()
_ALERT_NOW = timezone.make_aware(datetime.combine(_ALERT_DAY, dt_time(10, 35)))


class AlertEvaluationTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(code='600000', name='浦发银行')

    def test_price_above_triggers_once_per_day(self):
        MinuteBar.objects.create(
            stock=self.stock, datetime=timezone.make_aware(datetime.combine(_ALERT_DAY, dt_time(10, 30))),
            open=Decimal('10'), close=Decimal('10.50'), high=Decimal('10.6'),
            low=Decimal('9.9'), volume=100,
        )
        PriceAlert.objects.create(
            stock=self.stock, rule_type=PriceAlert.PRICE_ABOVE, threshold=Decimal('9.5'),
        )
        with patch('stocks.trading_calendar.is_trading_day', _trading_true):
            events = alerts.evaluate_alerts(now=_ALERT_NOW)
            self.assertEqual(len(events), 1)
            self.assertIn('上穿', events[0].message)
            # 同日不重复触发
            self.assertEqual(len(alerts.evaluate_alerts(now=_ALERT_NOW)), 0)

    def test_daily_pct_below_uses_positive_threshold(self):
        _make_quote(self.stock, Decimal('9.0'), -10.0, day=_ALERT_DAY)
        PriceAlert.objects.create(
            stock=self.stock, rule_type=PriceAlert.DAILY_PCT_BELOW, threshold=Decimal('5'),
        )
        with patch('stocks.trading_calendar.is_trading_day', _trading_true):
            events = alerts.evaluate_alerts(now=_ALERT_NOW)
        self.assertEqual(len(events), 1)
        self.assertIn('跌幅', events[0].message)

    def test_missing_data_skips_without_fabrication(self):
        PriceAlert.objects.create(
            stock=self.stock, rule_type=PriceAlert.PRICE_ABOVE, threshold=Decimal('9.5'),
        )
        PriceAlert.objects.create(
            stock=self.stock, rule_type=PriceAlert.DAILY_PCT_ABOVE, threshold=Decimal('3'),
        )
        with patch('stocks.trading_calendar.is_trading_day', _trading_true):
            events = alerts.evaluate_alerts(now=_ALERT_NOW)
        self.assertEqual(events, [])  # 无行情就不触发，绝不拿 0 凑数


class AlertApiTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(code='600000', name='浦发银行')

    def test_create_list_mark_read_and_delete(self):
        response = self.client.post(
            '/api/alerts/',
            json.dumps({
                'stock_id': self.stock.id,
                'rule_type': PriceAlert.PRICE_ABOVE,
                'threshold': '10.5',
                'note': '突破提醒',
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201)

        response = self.client.get('/api/alerts/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['items']), 1)
        self.assertEqual(response.data['unread_count'], 0)

        AlertEvent.objects.create(
            alert_id=response.data['items'][0]['id'], stock=self.stock,
            message='测试触发', trade_date=_BASE,
        )
        response = self.client.get('/api/alerts/events/?unread=1')
        self.assertEqual(len(response.data), 1)
        response = self.client.post('/api/alerts/events/read/', {})
        self.assertEqual(response.data['marked'], 1)
        self.assertEqual(self.client.get('/api/alerts/events/?unread=1').data, [])

        response = self.client.delete('/api/alerts/1/')
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.get('/api/alerts/').data['items'], [])

    def test_invalid_rule_type_returns_400(self):
        response = self.client.post(
            '/api/alerts/',
            json.dumps({'stock_id': self.stock.id, 'rule_type': 'bad', 'threshold': '1'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_negative_price_threshold_returns_400(self):
        response = self.client.post(
            '/api/alerts/',
            json.dumps({'stock_id': self.stock.id, 'rule_type': PriceAlert.PRICE_ABOVE, 'threshold': '-1'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)


class ScreenerPresetApiTests(TestCase):
    SPEC = {'logic': 'all', 'conditions': [{'field': 'change_pct', 'op': 'gt', 'value': 3}]}

    def test_create_list_duplicate_and_delete(self):
        response = self.client.post(
            '/api/screener/presets/',
            json.dumps({'name': '强势股', 'spec': self.SPEC}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201)

        response = self.client.get('/api/screener/presets/')
        user_presets = [p for p in response.data if not p['builtin']]
        self.assertEqual(len(user_presets), 1)
        self.assertEqual(user_presets[0]['spec'], self.SPEC)

        # 同名 409
        response = self.client.post('/api/screener/presets/', json.dumps({'name': '强势股', 'spec': self.SPEC}), content_type='application/json')
        self.assertEqual(response.status_code, 409)

        response = self.client.delete('/api/screener/presets/1/')
        self.assertEqual(response.status_code, 204)

    def test_invalid_spec_rejected_with_same_rules_as_execution(self):
        for bad in (
            {'conditions': []},
            {'logic': 'all', 'conditions': [{'field': 'nope', 'op': 'gt', 'value': 1}]},
            {'logic': 'all', 'conditions': [{'field': 'change_pct', 'op': 'nope', 'value': 1}]},
        ):
            with self.subTest(spec=bad):
                response = self.client.post('/api/screener/presets/', json.dumps({'name': 'x', 'spec': bad}), content_type='application/json')
                self.assertEqual(response.status_code, 400)


class DashboardCostTests(TestCase):
    def test_quote_row_carries_cost_fields(self):
        stock = Stock.objects.create(
            code='600000', name='浦发银行', cost_price=Decimal('10.000'), quantity=1000,
        )
        quote = _make_quote(stock, Decimal('11.0'), 10.0)
        row = _quote_row(stock, quote)
        self.assertEqual(row['cost_price'], Decimal('10.000'))
        self.assertEqual(row['quantity'], 1000)
        # 盈亏由前端按 (close - cost) * quantity 计算，后端不重复出口径

    def test_quote_row_without_cost_is_null(self):
        stock = Stock.objects.create(code='600000', name='浦发银行')
        row = _quote_row(stock, None)
        self.assertIsNone(row['cost_price'])
        self.assertIsNone(row['quantity'])


class MultiSourceRouterTests(SimpleTestCase):
    """_first_ok 多源路由：按优先级尝试、冷却跳过、全败返回 (None, None)。"""

    def setUp(self):
        from .market import _sources
        _sources._source_state.clear()

    def test_first_source_wins(self):
        from .market._sources import _first_ok

        result, source = _first_ok([
            (lambda: 'A', 'src_a'),
            (lambda: 'B', 'src_b'),
        ])
        self.assertEqual((result, source), ('A', 'src_a'))

    def test_falls_back_to_second_source(self):
        from .market._sources import _first_ok

        calls = []

        def bad():
            calls.append('a')
            raise RuntimeError('502')

        result, source = _first_ok([
            (bad, 'src_a'),
            (lambda: 'B', 'src_b'),
        ])
        self.assertEqual((result, source), ('B', 'src_b'))
        self.assertEqual(calls, ['a'])

    def test_all_fail_returns_none(self):
        from .market._sources import _first_ok

        result, source = _first_ok([
            (lambda: 1 / 0, 'src_x'),
            (lambda: None, 'src_y'),  # empty 也算失败
        ])
        self.assertIsNone(result)
        self.assertIsNone(source)

    def test_cooling_source_is_skipped(self):
        from .market import _sources
        from .market._sources import _first_ok

        _sources._source_mark_fail('src_a', 'boom')
        _sources._source_mark_fail('src_a', 'boom')  # 达到阈值进入冷却
        result, source = _first_ok([
            (lambda: 'A', 'src_a'),
            (lambda: 'B', 'src_b'),
        ])
        self.assertEqual((result, source), ('B', 'src_b'))


class SectorSnapshotFirstTests(TestCase):
    """5d/10d 只由快照供数：窗口齐全才可用，不足时如实不可用（DAT-15）。"""

    def test_5d_prefers_snapshot_when_window_complete(self):
        for offset in range(5):
            MarketDailySnapshot.objects.create(
                kind=MarketDailySnapshot.KIND_INDUSTRY_FF,
                trade_date=_BASE - timedelta(days=offset),
                payload=[{'name': '半导体', 'net': 10.0, 'change_pct': None}],
            )
        with patch('stocks.trading_calendar.is_trading_day', _trading_true), patch(
            'stocks.market.snapshots._expected_latest_date', return_value=_BASE,
        ), patch(
            'stocks.market.sectors.fetch_industry_fund_flow',
        ) as fetch_flow:
            result = market.get_sector_rotation(board='industry', period='5d')

        fetch_flow.assert_not_called()
        self.assertTrue(result['available'])
        self.assertEqual(result['items'][0]['net'], 50.0)
        self.assertIn('MarketDailySnapshot', result['meta']['source'])

    def test_5d_unavailable_when_snapshot_insufficient(self):
        """快照不足时如实不可用，不再退回东财原生排行（DAT-15：口径固定）。"""
        with patch('stocks.trading_calendar.is_trading_day', _trading_true), patch(
            'stocks.market.snapshots._expected_latest_date', return_value=_BASE,
        ), patch(
            'stocks.market.sectors.fetch_industry_fund_flow',
        ) as fetch_flow:
            result = market.get_sector_rotation(board='industry', period='5d')

        fetch_flow.assert_not_called()
        self.assertFalse(result['available'])
        self.assertEqual(result['basis'], 'ths_snapshot')
        self.assertIn('快照积累不足', result['message'])

    def test_5d_rejects_non_net_sort(self):
        with patch(
            'stocks.market.sectors.fetch_industry_fund_flow',
        ) as fetch_flow, self.assertRaisesMessage(ValueError, '仅支持按净额排序'):
            market.get_sector_rotation(
                board='industry', period='5d', sort='change_pct', order='desc',
            )
        fetch_flow.assert_not_called()


@patch('stocks.market.etf.fetch_holder_structure', new=_no_holders)
class EtfHistSourceRoutingTests(SimpleTestCase):
    """ETF 历史多源路由：东财优先，失败切新浪；meta 如实标注实际来源。"""

    def setUp(self):
        market._cache.clear()
        from .market import _sources
        _sources._source_state.clear()

    @patch('stocks.market.etf._normalized_etf_items')
    @patch('stocks.market.etf._fetch_etf_spot_df')
    def test_em_source_used_when_available(self, fetch_spot, normalized):
        normalized.return_value = [{
            'code': '510300', 'name': '沪深300ETF', 'exchange': 'SH',
            'share': 100, 'data_date': '2026-09-04',
        }]
        em_df = pd.DataFrame([
            {'日期': '2026-09-01', '开盘': 4.0, '收盘': 4.1, '最高': 4.2,
             '最低': 3.9, '成交量': 1000, '成交额': 4100000, '涨跌幅': 1.2, '换手率': 0.5},
        ])
        with patch('akshare.fund_etf_hist_em', return_value=em_df):
            data = market.get_etf_detail('510300', '1w')

        self.assertEqual(data['history']['meta']['source'], 'akshare.em_etf_hist')
        self.assertEqual(data['history']['count'], 1)
        self.assertEqual(data['history']['items'][0]['turnover'], 4100000)

    @patch('stocks.market.etf._normalized_etf_items')
    @patch('stocks.market.etf._fetch_etf_spot_df')
    def test_sina_fallback_when_em_fails(self, fetch_spot, normalized):
        normalized.return_value = [{
            'code': '510300', 'name': '沪深300ETF', 'exchange': 'SH',
            'share': 100, 'data_date': '2026-09-04',
        }]

        def em_boom(**kwargs):
            raise RuntimeError('502')

        # 日期相对今天：'1w' 区间按 today 回看 12 天截取，写死日期会随时间变红
        today = timezone.localdate()
        sina_df = pd.DataFrame([
            {'date': (today - timedelta(days=3)).isoformat(), 'open': 4.0, 'high': 4.2,
             'low': 3.9, 'close': 4.1, 'volume': 1000},
            {'date': (today - timedelta(days=2)).isoformat(), 'open': 4.1, 'high': 4.3,
             'low': 4.0, 'close': 4.2, 'volume': 1100},
        ])
        with patch('akshare.fund_etf_hist_em', em_boom), patch(
            'akshare.fund_etf_hist_sina', return_value=sina_df,
        ) as sina:
            data = market.get_etf_detail('510300', '1w')

        sina.assert_called_once_with(symbol='sh510300')
        self.assertEqual(data['history']['meta']['source'], 'akshare.sina_etf_hist')
        self.assertTrue(data['history']['available'])
        self.assertEqual(data['history']['count'], 2)
        # 新浪无成交额/涨跌幅/换手率，如实为 None（不推算凑数）
        item = data['history']['items'][0]
        self.assertEqual(item['close'], 4.1)
        self.assertIsNone(item['turnover'])
        self.assertIsNone(item['change_pct'])
        self.assertIsNone(item['turnover_rate'])

    @patch('stocks.market.etf._normalized_etf_items')
    @patch('stocks.market.etf._fetch_etf_spot_df')
    def test_hist_unavailable_when_all_sources_fail(self, fetch_spot, normalized):
        normalized.return_value = [{
            'code': '510300', 'name': '沪深300ETF', 'exchange': 'SH',
            'share': 100, 'data_date': '2026-09-04',
        }]
        with patch('akshare.fund_etf_hist_em', side_effect=RuntimeError('502')), patch(
            'akshare.fund_etf_hist_sina', side_effect=RuntimeError('timeout'),
        ):
            data = market.get_etf_detail('510300', '1w')

        self.assertFalse(data['history']['available'])
        self.assertIsNone(data['price_performance']['return_5d'])


@patch('stocks.market.etf.fetch_holder_structure', new=_no_holders)
class EtfCustomRangeTests(SimpleTestCase):
    """ETF 详情自定义起止区间：参数校验 + 区间统计。"""

    def setUp(self):
        market._cache.clear()

    def _quote_patch(self):
        return patch('stocks.market.etf._normalized_etf_items', return_value=[{
            'code': '510300', 'name': '沪深300ETF', 'exchange': 'SH',
            'share': 100, 'data_date': '2026-09-04',
        }])

    def test_custom_range_fetch_and_stats(self):
        em_df = pd.DataFrame([
            {'日期': '2026-08-01', '开盘': 4.0, '收盘': 4.0, '最高': 4.1, '最低': 3.9,
             '成交量': 1000, '成交额': 4000000, '涨跌幅': 0.0, '换手率': 0.5},
            {'日期': '2026-08-15', '开盘': 4.1, '收盘': 4.4, '最高': 4.5, '最低': 4.0,
             '成交量': 2000, '成交额': 8800000, '涨跌幅': 2.0, '换手率': 0.8},
        ])
        with self._quote_patch(), patch('akshare.fund_etf_hist_em', return_value=em_df) as em:
            data = market.get_etf_detail(
                '510300', 'custom', start_date='2026-08-01', end_date='2026-08-31')

        self.assertEqual(em.call_args.kwargs['start_date'], '20260801')
        self.assertEqual(em.call_args.kwargs['end_date'], '20260831')
        # start_date/end_date 是实际数据首尾日（可能窄于请求区间）
        self.assertEqual(data['history']['start_date'], '2026-08-01')
        self.assertEqual(data['history']['end_date'], '2026-08-15')
        stats = data['history']['stats']
        self.assertEqual(stats['count'], 2)
        self.assertEqual(stats['change_pct'], 10.0)  # 4.0 -> 4.4
        self.assertEqual(stats['high'], 4.5)
        self.assertEqual(stats['low'], 3.9)
        self.assertEqual(stats['avg_turnover'], 6400000.0)

    def test_custom_range_requires_start(self):
        with self._quote_patch():
            with self.assertRaisesMessage(ValueError, 'start_date'):
                market.get_etf_detail('510300', 'custom')

    def test_custom_range_bad_format(self):
        with self._quote_patch():
            with self.assertRaisesMessage(ValueError, 'YYYY-MM-DD'):
                market.get_etf_detail('510300', 'custom', start_date='20260801')

    def test_custom_range_start_after_end(self):
        with self._quote_patch():
            with self.assertRaisesMessage(ValueError, 'start_date'):
                market.get_etf_detail(
                    '510300', 'custom', start_date='2026-08-31', end_date='2026-08-01')

    def test_custom_range_too_long(self):
        with self._quote_patch():
            with self.assertRaisesMessage(ValueError, '最长'):
                market.get_etf_detail(
                    '510300', 'custom', start_date='2020-01-01', end_date='2026-08-31')

    def test_custom_range_meta_source_matches_actual_source(self):
        """东财失败切新浪时，custom 区间的 meta 来源不能因缓存 key 不一致而误标。"""
        sina_df = pd.DataFrame([
            {'date': '2026-08-03', 'open': 4.0, 'high': 4.1, 'low': 3.9,
             'close': 4.0, 'volume': 1000},
        ])

        def em_boom(**kwargs):
            raise RuntimeError('502')

        with self._quote_patch(), patch('akshare.fund_etf_hist_em', em_boom), patch(
            'akshare.fund_etf_hist_sina', return_value=sina_df,
        ):
            data = market.get_etf_detail(
                '510300', 'custom', start_date='2026-08-01', end_date='2026-08-31')
        self.assertEqual(data['history']['meta']['source'], 'akshare.sina_etf_hist')
        self.assertIsNone(data['history']['items'][0]['turnover'])

    def test_preset_range_still_accepted(self):
        em_df = pd.DataFrame([
            {'日期': '2026-09-01', '开盘': 4.0, '收盘': 4.1, '最高': 4.2, '最低': 3.9,
             '成交量': 1000, '成交额': 4100000, '涨跌幅': 1.2, '换手率': 0.5},
        ])
        with self._quote_patch(), patch('akshare.fund_etf_hist_em', return_value=em_df):
            data = market.get_etf_detail('510300', '1w')
        self.assertEqual(data['history']['range'], '1w')
        self.assertEqual(data['history']['stats']['count'], 1)


class StockGroupApiTests(TestCase):
    def test_group_crud_assign_and_ungroup_on_delete(self):
        stock = Stock.objects.create(code='600000', name='浦发银行')

        response = self.client.post('/api/stock-groups/', json.dumps({'name': '白马'}), content_type='application/json')
        self.assertEqual(response.status_code, 201)
        group_id = response.data['id']

        # 重名 409
        response = self.client.post('/api/stock-groups/', json.dumps({'name': '白马'}), content_type='application/json')
        self.assertEqual(response.status_code, 409)

        # 把股票划入分组
        response = self.client.patch(f'/api/stocks/{stock.id}/', json.dumps({'group': group_id}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['group'], group_id)

        # 列表带活跃股票计数；dashboard 行携带分组信息
        self.assertEqual(self.client.get('/api/stock-groups/').data[0]['stock_count'], 1)
        dashboard_row = self.client.get('/api/dashboard/').data[0]
        self.assertEqual(dashboard_row['group_id'], group_id)
        self.assertEqual(dashboard_row['group_name'], '白马')

        # 重命名
        response = self.client.patch(f'/api/stock-groups/{group_id}/', json.dumps({'name': '价值'}), content_type='application/json')
        self.assertEqual(response.data['name'], '价值')

        # 删除分组 → 股票自动回到未分组
        response = self.client.delete(f'/api/stock-groups/{group_id}/')
        self.assertEqual(response.status_code, 204)
        dashboard_row = self.client.get('/api/dashboard/').data[0]
        self.assertIsNone(dashboard_row['group_id'])

    def test_group_name_required(self):
        response = self.client.post('/api/stock-groups/', json.dumps({'name': '  '}), content_type='application/json')
        self.assertEqual(response.status_code, 400)


@patch('stocks.trading_calendar.is_trading_day', _trading_true)
@patch('akshare.fund_etf_scale_sse', new=Mock(return_value=pd.DataFrame()))
class MarketFundFlowSnapshotTests(TestCase):
    """大盘主力资金流快照：收盘落库 + 窗口接口在上游挂掉时用快照兜底。"""

    def setUp(self):
        market._cache.clear()

    @staticmethod
    def _kline(date_str, main):
        # fflow daykline 行：日期,主力,小,中,大,超大,占比...,收盘,涨跌幅
        rest = ','.join(['1.0'] * 5 + ['0.1'] * 5 + ['10.0', '1.0'])
        return f'{date_str},{main},1.0,1.0,1.0,1.0,{rest}'

    def test_save_writes_market_ff_row_for_today(self):
        def fake_klines(secid):
            main = -100.0 if secid == '1.000001' else -50.0
            return [self._kline(_BASE.isoformat(), main)]

        with patch(
            'stocks.market.etf_flow.fetch_flow_klines', side_effect=fake_klines,
        ), patch(
            'stocks.market.sectors.fetch_industry_fund_flow', return_value=[],
        ), patch(
            'stocks.market.sectors.fetch_concept_fund_flow', return_value=[],
        ), patch(
            'stocks.market.etf._normalized_etf_items', return_value=[],
        ), patch('stocks.market.snapshots._etf_spot_fresh_for', return_value=True):
            saved = snapshots.save_daily_snapshots(trade_date=_BASE)

        self.assertEqual(saved[MarketDailySnapshot.KIND_MARKET_FF], 1)
        row = MarketDailySnapshot.objects.get(
            kind=MarketDailySnapshot.KIND_MARKET_FF, trade_date=_BASE,
        )
        self.assertEqual(row.payload[0]['main_net'], -150.0)  # 沪深同日合并

    def test_partial_market_not_written_as_total(self):
        def fake_klines(secid):
            if secid == '0.399001':
                raise RuntimeError('down')  # 深市缺失：不得把沪市单独冒充两市合计
            return [self._kline(_BASE.isoformat(), -100.0)]

        with patch(
            'stocks.market.etf_flow.fetch_flow_klines', side_effect=fake_klines,
        ), patch(
            'stocks.market.sectors.fetch_industry_fund_flow', return_value=[],
        ), patch(
            'stocks.market.sectors.fetch_concept_fund_flow', return_value=[],
        ), patch(
            'stocks.market.etf._normalized_etf_items', return_value=[],
        ), patch('stocks.market.snapshots._etf_spot_fresh_for', return_value=True):
            saved = snapshots.save_daily_snapshots(trade_date=_BASE)
        self.assertNotIn(MarketDailySnapshot.KIND_MARKET_FF, saved)
        self.assertFalse(
            MarketDailySnapshot.objects.filter(kind=MarketDailySnapshot.KIND_MARKET_FF).exists()
        )

    def test_window_falls_back_to_snapshots_when_upstream_down(self):
        for offset, net in ((0, 100.0), (1, -50.0)):
            MarketDailySnapshot.objects.create(
                kind=MarketDailySnapshot.KIND_MARKET_FF,
                trade_date=_BASE - timedelta(days=offset),
                payload=[{'date': (_BASE - timedelta(days=offset)).isoformat(),
                          'main_net': net, 'super_net': None, 'large_net': None,
                          'mid_net': None, 'small_net': None}],
            )
        from stocks.market.periods import PeriodWindow

        window = PeriodWindow('custom', _BASE - timedelta(days=1), _BASE)
        with patch(
            'stocks.market.etf_flow.fetch_flow_klines',
            side_effect=RuntimeError('RemoteDisconnected'),
        ):
            data = market.get_market_fund_flow_window(window)

        self.assertTrue(data['available'])
        self.assertEqual(data['summary']['days'], 2)
        self.assertEqual(data['summary']['total_main_net'], 50.0)
        self.assertIn('快照', data['message'])
        self.assertIn('本站日度快照', data['meta']['source'])

    def test_window_snapshot_fills_missing_upstream_dates(self):
        # 上游只有最新一天；快照补齐更早一天
        MarketDailySnapshot.objects.create(
            kind=MarketDailySnapshot.KIND_MARKET_FF,
            trade_date=_BASE - timedelta(days=1),
            payload=[{'date': (_BASE - timedelta(days=1)).isoformat(),
                      'main_net': -50.0, 'super_net': None, 'large_net': None,
                      'mid_net': None, 'small_net': None}],
        )
        from stocks.market.periods import PeriodWindow

        window = PeriodWindow('custom', _BASE - timedelta(days=1), _BASE)
        def fake_klines(secid):
            main = 70.0 if secid == '1.000001' else 30.0  # 沪深同日合计 100
            return [f'{_BASE.isoformat()},{main},0.0,0.0,0.0,0.0,0,0,0,0,0,3000.0,0.5']

        with patch('stocks.market.etf_flow.fetch_flow_klines', side_effect=fake_klines):
            data = market.get_market_fund_flow_window(window)

        self.assertTrue(data['available'])
        self.assertEqual(data['summary']['days'], 2)
        self.assertEqual(data['summary']['total_main_net'], 50.0)
        self.assertEqual(data['summary']['inflow_days'], 1)
        self.assertEqual(data['summary']['outflow_days'], 1)
        self.assertEqual(data['message'], '')  # 上游可用时不提示降级


class IndexValuationTests(SimpleTestCase):
    """宽基指数估值分位（乐咕月度 PE）：分位计算与如实降级。"""

    def setUp(self):
        market._cache.clear()
        from .market import _sources
        _sources._source_state.clear()  # 清共享源冷却状态，避免用例间串扰

    @staticmethod
    def _pe_df(pes):
        return pd.DataFrame({
            '日期': [f'2026-0{i+1}-01' for i in range(len(pes))],
            '滚动市盈率': pes,
        })

    def test_percentile_computed_and_all_indices_present(self):
        pes = [10.0, 20.0, 15.0]
        with patch('akshare.stock_index_pe_lg', side_effect=lambda symbol: self._pe_df(pes)):
            d = market.indices.fetch_index_valuations(force=True)
        self.assertTrue(d['available'])
        self.assertEqual({i['name'] for i in d['items']}, {'沪深300', '上证50', '中证500', '中证1000'})
        it = d['items'][0]
        self.assertEqual(it['pe'], 15.0)
        self.assertAlmostEqual(it['pe_percentile'], 33.3)  # 历史中小于当前的占比 1/3
        self.assertEqual(it['history_count'], 3)

    def test_partial_failure_skips_only_failed_index(self):
        def fake(symbol):
            if symbol == '中证1000':
                raise RuntimeError('boom')
            return self._pe_df([10.0, 12.0])

        with patch('akshare.stock_index_pe_lg', side_effect=fake):
            d = market.indices.fetch_index_valuations(force=True)
        self.assertTrue(d['available'])
        self.assertEqual({i['name'] for i in d['items']}, {'沪深300', '上证50', '中证500'})

    def test_all_fail_degrades_honestly(self):
        with patch('akshare.stock_index_pe_lg', side_effect=RuntimeError('boom')):
            d = market.indices.fetch_index_valuations(force=True)
        self.assertFalse(d['available'])
        self.assertEqual(d['items'], [])
        self.assertTrue(d['message'])


class _FakeDate(date):
    """固定「今天」，使两融/池情绪的按日回退测试与运行日期无关。"""

    @classmethod
    def today(cls):
        return date(2026, 9, 6)  # 周日：今天应被跳过、回退到最近交易日


class ZtSentimentTests(SimpleTestCase):
    """涨跌停池情绪：池口径指标、非交易日回退与如实降级。"""

    def setUp(self):
        market._cache.clear()
        from .market import _sources
        _sources._source_state.clear()  # 清共享源冷却状态，避免用例间串扰

    @staticmethod
    def _zt_df():
        return pd.DataFrame({
            '代码': ['605577', '605398'], '名称': ['龙版传媒', '新炬网络'],
            '涨跌幅': [9.97, 9.98], '最新价': [15.55, 26.66],
            '成交额': [5.0e8, 1.0e8], '连板数': [5, 2], '所属行业': ['出版', 'IT服务'],
        })

    def test_metrics_top_and_seal_rate(self):
        with patch('stocks.trading_calendar.is_trading_day', return_value=True), \
             patch('stocks.market.sentiment._date', _FakeDate), \
             patch('stocks.trading_calendar._date', _FakeDate), \
             patch('akshare.stock_zt_pool_em', return_value=self._zt_df()), \
             patch('akshare.stock_zt_pool_dtgc_em', return_value=pd.DataFrame({'x': [1]})), \
             patch('akshare.stock_zt_pool_zbgc_em', return_value=pd.DataFrame({'x': [1, 2, 3]})):
            d = market.sentiment.fetch_zt_sentiment(force=True)
        self.assertTrue(d['available'])
        self.assertEqual(d['zt_count'], 2)
        self.assertEqual(d['dt_count'], 1)
        self.assertEqual(d['zb_count'], 3)
        self.assertEqual(d['seal_rate'], 40.0)  # 2/(2+3)
        self.assertEqual(d['max_lb'], 5)
        self.assertEqual(d['lb_count'], 2)
        self.assertTrue(d['is_live'])
        self.assertEqual(d['top'][0]['name'], '龙版传媒')  # 连板数降序
        self.assertEqual(d['top'][0]['lb'], 5)

    def test_fallback_to_recent_day_when_today_empty(self):
        seq = iter([pd.DataFrame(), self._zt_df()])

        with patch('stocks.trading_calendar.is_trading_day', return_value=True), \
             patch('stocks.market.sentiment._date', _FakeDate), \
             patch('stocks.trading_calendar._date', _FakeDate), \
             patch('akshare.stock_zt_pool_em', side_effect=lambda date: next(seq)), \
             patch('akshare.stock_zt_pool_dtgc_em', return_value=pd.DataFrame()), \
             patch('akshare.stock_zt_pool_zbgc_em', return_value=pd.DataFrame()):
            d = market.sentiment.fetch_zt_sentiment(force=True)
        self.assertTrue(d['available'])
        self.assertFalse(d['is_live'])
        self.assertEqual(d['date'], '2026-09-05')
        self.assertIn('收盘口径', d['message'])

    def test_all_fail_degrades_honestly(self):
        with patch('stocks.trading_calendar.is_trading_day', return_value=True), \
             patch('stocks.market.sentiment._date', _FakeDate), \
             patch('stocks.trading_calendar._date', _FakeDate), \
             patch('akshare.stock_zt_pool_em', side_effect=RuntimeError('boom')):
            d = market.sentiment.fetch_zt_sentiment(force=True)
        self.assertFalse(d['available'])
        self.assertTrue(d['message'])


class MarginBalanceTests(SimpleTestCase):
    """沪深两融余额：同日口径合计、深市未披露降级沪市与如实降级。"""

    def setUp(self):
        market._cache.clear()
        from .market import _sources
        _sources._source_state.clear()

    @staticmethod
    def _sse_df():
        # 上游按日期倒序返回（最新在前），解析层应自行排序
        return pd.DataFrame({
            '信用交易日期': [20260904, 20260903, 20260902],
            '融资余额': [1.3340e12, 1.3320e12, 1.3300e12],
            '融券余量金额': [3.0e10, 2.93e10, 2.9e10],
            '融资融券余额': [1.3640e12, 1.3613e12, 1.3500e12],
        })

    @staticmethod
    def _sz_df(total, rz):
        return pd.DataFrame({
            '融资买入额': [710.12], '融资余额': [rz], '融券卖出量': [0.28],
            '融券余量': [12.18], '融券余额': [total - rz], '融资融券余额': [total],
        })

    def test_sh_sz_same_day_total_and_change(self):
        def fake_sz(date):
            if date == '20260904':
                return self._sz_df(12868.92, 12762.74)
            if date == '20260903':
                return self._sz_df(12900.00, 12780.00)
            raise ValueError('该日未披露')  # 深交所对未披露日期直接抛错

        with patch('stocks.trading_calendar.is_trading_day', return_value=True), \
             patch('stocks.market.sentiment._date', _FakeDate), \
             patch('stocks.trading_calendar._date', _FakeDate), \
             patch('akshare.stock_margin_sse', return_value=self._sse_df()), \
             patch('akshare.stock_margin_szse', side_effect=fake_sz):
            d = market.sentiment.fetch_margin_balance(force=True)
        self.assertTrue(d['available'])
        self.assertEqual(d['scope'], 'sh_sz')
        self.assertEqual(d['date'], '2026-09-04')
        self.assertEqual(d['total'], 26508.92)  # 13640.0 + 12868.92
        self.assertEqual(d['sz_total'], 12868.92)
        self.assertEqual(d['chg_1d'], -4.08)  # 26508.92 - (13613.0 + 12900.0)
        self.assertEqual(d['chg_pct_1d'], -0.02)
        self.assertFalse(d.get('message'))

    def test_sz_unpublished_degrades_to_sh_only(self):
        with patch('stocks.trading_calendar.is_trading_day', return_value=True), \
             patch('stocks.market.sentiment._date', _FakeDate), \
             patch('stocks.trading_calendar._date', _FakeDate), \
             patch('akshare.stock_margin_sse', return_value=self._sse_df()), \
             patch('akshare.stock_margin_szse', side_effect=ValueError('未披露')):
            d = market.sentiment.fetch_margin_balance(force=True)
        self.assertTrue(d['available'])
        self.assertEqual(d['scope'], 'sh_only')
        self.assertEqual(d['total'], 13640.0)
        self.assertIsNone(d['sz_total'])
        self.assertEqual(d['chg_1d'], 27.0)  # 沪市口径：13640.0 - 13613.0
        self.assertIn('沪市口径', d['message'])

    def test_all_fail_degrades_honestly(self):
        with patch('stocks.trading_calendar.is_trading_day', return_value=True), \
             patch('stocks.market.sentiment._date', _FakeDate), \
             patch('stocks.trading_calendar._date', _FakeDate), \
             patch('akshare.stock_margin_sse', side_effect=RuntimeError('boom')), \
             patch('akshare.stock_margin_szse', side_effect=RuntimeError('boom')):
            d = market.sentiment.fetch_margin_balance(force=True)
        self.assertFalse(d['available'])
        self.assertTrue(d['message'])


class SnapshotSseSharePreferenceTests(TestCase):
    """etf_share 快照沪市份额官方优先：上交所文件覆盖/补齐，东财兜底。"""

    def setUp(self):
        market._cache.clear()

    @staticmethod
    def _sse_df():
        return pd.DataFrame([
            {'基金代码': '510300', '基金简称': '300ETF', '基金份额': 233.0},
            {'基金代码': '588000', '基金简称': '科创50ETF', '基金份额': 99.0},
        ])

    def _save_with(self, em_items, sse):
        """sse: DataFrame，或 Exception 实例（模拟上游拉取失败）。"""
        sse_kwargs = (
            {'side_effect': sse} if isinstance(sse, Exception) else {'return_value': sse}
        )
        with patch('stocks.trading_calendar.is_trading_day', _trading_true), patch(
            'stocks.market.sectors.fetch_industry_fund_flow', return_value=[],
        ), patch(
            'stocks.market.sectors.fetch_concept_fund_flow', return_value=[],
        ), patch(
            'stocks.market.etf_flow.fetch_flow_klines', side_effect=RuntimeError('down'),
        ), patch(
            'stocks.market.etf._normalized_etf_items', return_value=em_items,
        ), patch('stocks.market.snapshots._etf_spot_fresh_for', return_value=True), \
                patch('akshare.fund_etf_scale_sse', **sse_kwargs):
            return snapshots.save_daily_snapshots(trade_date=_BASE)

    def _saved_etf_rows(self):
        row = MarketDailySnapshot.objects.get(
            kind=MarketDailySnapshot.KIND_ETF_SHARE, trade_date=_BASE,
        )
        return {r['code']: r for r in row.payload}

    def test_sse_overrides_sh_share_and_fills_missing(self):
        em = [
            {'code': '510300', 'name': '沪深300ETF', 'share': 230.0, 'market_cap': 900.0, 'turnover': 10.0},
            {'code': '159915', 'name': '创业板ETF', 'share': 120.0, 'market_cap': 500.0, 'turnover': 8.0},
        ]
        saved = self._save_with(em, self._sse_df())
        self.assertEqual(saved[MarketDailySnapshot.KIND_ETF_SHARE], 3)  # 沪 2 + 深 1
        rows = self._saved_etf_rows()
        self.assertEqual(rows['510300']['share'], 233.0)  # 官方份额覆盖
        self.assertEqual(rows['510300']['market_cap'], 900.0)  # 市值/成交额保留东财口径
        self.assertEqual(rows['159915']['share'], 120.0)  # 深市不受影响
        self.assertEqual(rows['588000']['share'], 99.0)  # 东财缺份额的沪市 ETF 官方补齐
        self.assertIsNone(rows['588000']['market_cap'])

    def test_sse_unavailable_falls_back_to_em(self):
        em = [{'code': '159915', 'name': '创业板ETF', 'share': 120.0, 'market_cap': 500.0, 'turnover': 8.0}]

        # 当日官方文件未出（空表）
        saved = self._save_with(em, pd.DataFrame())
        self.assertEqual(saved[MarketDailySnapshot.KIND_ETF_SHARE], 1)
        self.assertEqual(self._saved_etf_rows()['159915']['share'], 120.0)

        # 官方源拉取失败
        MarketDailySnapshot.objects.all().delete()
        saved = self._save_with(em, RuntimeError('boom'))
        self.assertEqual(saved[MarketDailySnapshot.KIND_ETF_SHARE], 1)
        self.assertEqual(self._saved_etf_rows()['159915']['share'], 120.0)

    def test_em_empty_uses_sse_only(self):
        saved = self._save_with([], self._sse_df())
        self.assertEqual(saved[MarketDailySnapshot.KIND_ETF_SHARE], 2)
        rows = self._saved_etf_rows()
        self.assertEqual(set(rows), {'510300', '588000'})
        self.assertTrue(all(r['market_cap'] is None for r in rows.values()))


class FlowKlinesMirrorFallbackTests(SimpleTestCase):
    """fflow 抓取双镜像：push2his 被掐时退 push2delay（仅当日一根兜底）。"""

    def setUp(self):
        market._cache.clear()
        from .market import _sources
        _sources._source_state.clear()

    @staticmethod
    def _resp(klines):
        return type('R', (), {'json': lambda self: {'data': {'klines': klines}}})()

    def test_falls_back_to_push2delay_and_labels_source(self):
        urls = []

        def fake_get(url, **kwargs):
            urls.append(url)
            if 'push2his' in url:
                raise RuntimeError('Network is unreachable')
            return self._resp(['2026-09-07,-1.0e10,1e8,1e8,1e8,1e8,1,1,1,1,1,10.0,1.0'])

        with patch('stocks.market.etf_flow.requests.get', side_effect=fake_get), \
             patch('stocks.market.etf_flow.time.sleep'):
            klines = market.etf_flow.fetch_flow_klines('1.000001')
        self.assertEqual(len(klines), 1)
        self.assertEqual(
            market.etf_flow._FLOW_LAST_HOST['host'], 'push2delay.eastmoney.com',
        )
        self.assertIn('push2his', urls[0])
        self.assertIn('push2delay', urls[-1])
        self.assertIn('push2delay', market.etf_flow._flow_source())

    def test_both_mirrors_fail_raises_with_host_detail(self):
        with patch('stocks.market.etf_flow.requests.get', side_effect=RuntimeError('down')), \
             patch('stocks.market.etf_flow.time.sleep'):
            with self.assertRaises(RuntimeError) as cm:
                market.etf_flow.fetch_flow_klines('1.000001')
        self.assertIn('push2his', str(cm.exception))
        self.assertIn('push2delay', str(cm.exception))


class ThsFundFlowTests(SimpleTestCase):
    """同花顺板块资金流自实现抓取：分页解析、% 清洗与页面改版时大声失败。"""

    def setUp(self):
        market._cache.clear()
        from .market import _sources
        _sources._source_state.clear()

    class _FakeResp:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            return None

    @staticmethod
    def _html(rows, page_info='1/2'):
        trs = ''
        for i in range(rows):
            cells = [
                str(i + 1), '养殖业', '3043.89', '4.36%', '54.33亿', '38.31亿',
                '16.02亿', '36', '罗牛山', '10.05%', '10.88',
            ]
            trs += '<tr>' + ''.join(f'<td>{c}</td>' for c in cells) + '</tr>'
        return (
            '<html><body><span class="page_info">' + page_info + '</span>'
            '<table>' + trs + '</table></body></html>'
        )

    def test_pagination_parse_and_pct_strip(self):
        html = self._html(10)
        seq = [html, self._html(10, page_info='1/2')]

        def fake_get(url, **kwargs):
            self.assertIn('data.10jqka.com.cn', url)
            return self._FakeResp(seq.pop(0))

        with patch('stocks.market.sectors._ths_token_headers', return_value={}), \
             patch('stocks.market.sectors.requests.get', side_effect=fake_get):
            df = market.sectors._ths_fund_flow('industry')
        self.assertEqual(len(df), 20)
        self.assertEqual(list(df.columns)[:2], ['行业', '行业指数'])  # 序号已丢弃
        self.assertEqual(df['净额'].iloc[0], '16.02亿')
        self.assertEqual(df['行业-涨跌幅'].iloc[0], '4.36')  # % 已清洗
        self.assertEqual(df['领涨股-涨跌幅'].iloc[0], '10.05')

    def test_page_layout_change_fails_loud_and_cooldowns(self):
        bad = '<html><body><table>' + ''.join(f'<td>{i}</td>' for i in range(8)) + '</table></body></html>'
        with patch('stocks.market.sectors._ths_token_headers', return_value={}), \
             patch('stocks.market.sectors.requests.get', return_value=type(
                 'R', (), {'text': bad, 'raise_for_status': lambda self: None},
             )()):
            df = market.sectors._ths_fund_flow_df('industry', 'ths_test_src')
        self.assertIsNone(df)
        from .market import _sources
        self.assertGreaterEqual(_sources._source_state['ths_test_src']['fails'], 1)


class SnapshotCloseGuardTests(TestCase):
    """15:30 守卫：交易日收盘前写当日快照会把上一日盘面标成今天（踩坑回归）。"""

    def _save_with(self, fake_now, trade_date=None):
        aware = timezone.make_aware(fake_now)
        with patch('stocks.market.snapshots.timezone') as tz, \
             patch('stocks.trading_calendar.is_trading_day', _trading_true), \
             patch(
                 'stocks.market.sectors._ths_fund_flow_df',
                 return_value=pd.DataFrame([{'行业': 'A', '净额': 1.0}]),
             ), patch(
                 'stocks.market.sectors._safe_df_call', return_value=None,
             ), patch(
                 'stocks.market.etf._normalized_etf_items', return_value=[],
             ), patch(
                 'stocks.market.etf_flow.fetch_flow_klines', side_effect=RuntimeError('down'),
             ), patch('stocks.market.snapshots._etf_spot_fresh_for', return_value=True), \
                patch('akshare.fund_etf_scale_sse', return_value=pd.DataFrame()):
            tz.localtime.return_value = aware
            return snapshots.save_daily_snapshots(trade_date=trade_date or aware.date())

    def test_before_close_skips_same_day(self):
        saved = self._save_with(datetime(2026, 9, 7, 10, 0))
        self.assertEqual(saved, {})
        self.assertFalse(
            MarketDailySnapshot.objects.filter(trade_date='2026-09-07').exists()
        )

    def test_after_close_writes(self):
        saved = self._save_with(datetime(2026, 9, 7, 16, 0))
        self.assertEqual(saved[MarketDailySnapshot.KIND_INDUSTRY_FF], 1)

    def test_past_date_not_blocked_by_close_guard(self):
        # 补写 09-07 历史（当前时刻已是 09-08 上午）不受守卫限制
        saved = self._save_with(datetime(2026, 9, 8, 10, 0), trade_date=datetime(2026, 9, 7).date())
        self.assertEqual(saved[MarketDailySnapshot.KIND_INDUSTRY_FF], 1)


class SnapshotSelfCheckTests(TestCase):
    """快照落库自检：收盘后各 kind 均应有非空行；缺了如实列出。"""

    def _now(self):
        return timezone.make_aware(datetime(2026, 9, 7, 16, 0))

    def _patch_now(self):
        tz = patch('stocks.market.snapshots.timezone')
        mock = tz.start()
        mock.localtime.return_value = self._now()
        mock.localdate.return_value = self._now().date()
        self.addCleanup(tz.stop)
        return mock

    def _write(self, kind, rows):
        MarketDailySnapshot.objects.create(
            kind=kind, trade_date=self._now().date(), payload=rows,
        )

    def test_all_present_returns_empty(self):
        self._patch_now()
        with patch('stocks.trading_calendar.is_trading_day', _trading_true):
            self._write(MarketDailySnapshot.KIND_INDUSTRY_FF, [{'name': 'A', 'net': 1.0}])
            self._write(MarketDailySnapshot.KIND_CONCEPT_FF, [{'name': 'B', 'net': 2.0}])
            self._write(MarketDailySnapshot.KIND_MARKET_FF, [{'date': '2026-09-07', 'main_net': 1.0}])
            self._write(MarketDailySnapshot.KIND_ETF_SHARE, [{'code': '510300'}] * 150)
            missing = snapshots.verify_daily_snapshots()
        self.assertEqual(missing, [])

    def test_missing_and_thin_kinds_listed(self):
        self._patch_now()
        with patch('stocks.trading_calendar.is_trading_day', _trading_true):
            self._write(MarketDailySnapshot.KIND_INDUSTRY_FF, [{'name': 'A', 'net': 1.0}])
            self._write(MarketDailySnapshot.KIND_ETF_SHARE, [{'code': '510300'}] * 3)  # 行数骤减
            missing = snapshots.verify_daily_snapshots()
        self.assertEqual(
            sorted(missing),
            sorted([MarketDailySnapshot.KIND_CONCEPT_FF, MarketDailySnapshot.KIND_MARKET_FF,
                    MarketDailySnapshot.KIND_ETF_SHARE]),
        )

    def test_before_close_or_weekend_returns_none(self):
        mock = self._patch_now()
        mock.localtime.return_value = timezone.make_aware(datetime(2026, 9, 7, 10, 0))
        with patch('stocks.trading_calendar.is_trading_day', _trading_true):
            self.assertIsNone(snapshots.verify_daily_snapshots())
        mock.localtime.return_value = timezone.make_aware(datetime(2026, 9, 7, 16, 0))
        with patch('stocks.trading_calendar.is_trading_day', return_value=False):
            self.assertIsNone(snapshots.verify_daily_snapshots())


class PushMessageTests(SimpleTestCase):
    """通用推送助手：未配置静默跳过，配置后按 Server酱格式 POST。"""

    def setUp(self):
        from .market import _sources
        _sources._source_state.clear()

    def test_no_url_is_silent_noop(self):
        from .alerts import push_message
        with patch('stocks.alerts.settings') as mock_settings:
            mock_settings.STOCKTRACE_PUSH_URL = ''
            self.assertFalse(push_message('t', 'd'))

    def test_posts_title_and_desp(self):
        from .alerts import push_message
        with patch('stocks.alerts.settings') as mock_settings, \
             patch('stocks.alerts.requests.post') as mock_post:
            mock_settings.STOCKTRACE_PUSH_URL = 'https://push.example/x'
            mock_post.return_value.raise_for_status.return_value = None
            self.assertTrue(push_message('标题', '内容'))
        kwargs = mock_post.call_args.kwargs
        self.assertEqual(kwargs['json'], {'title': '标题', 'desp': '内容'})


class StockMarginTests(SimpleTestCase):
    """个股两融明细（交易所官方）：单位归一、日变化与非标的降级。"""

    def setUp(self):
        market._cache.clear()
        from .market import _sources
        _sources._source_state.clear()

    @staticmethod
    def _sse_df(rz_yuan):
        return pd.DataFrame([{
            '信用交易日期': 20260904, '标的证券代码': '600519', '标的证券简称': '贵州茅台',
            '融资余额': rz_yuan, '融资买入额': 1.4e8, '融资偿还额': 1.6e8,
            '融券余量': 110419, '融券卖出量': 6700, '融券偿还量': 1000,
        }])

    @staticmethod
    def _sz_df(rz_yuan, rq_yuan, total_yuan):
        return pd.DataFrame([{
            '证券代码': '000001', '证券简称': '平安银行', '融资买入额': 1.2e8,
            '融资余额': rz_yuan, '融券卖出量': 386672, '融券余量': 7432137,
            '融券余额': rq_yuan, '融资融券余额': total_yuan,
        }])

    def test_sse_path_unit_and_change(self):
        def fake_sse(date):
            return self._sse_df(1.70e10) if date == '20260903' else self._sse_df(1.73e10)

        with patch('stocks.market.margin_stock._recent_day_candidates',
                   return_value=[date(2026, 9, 4)]), \
             patch('stocks.market.margin_stock._prev_trading_day',
                   return_value=date(2026, 9, 3)), \
             patch('akshare.stock_margin_detail_sse', side_effect=fake_sse):
            d = market.fetch_stock_margin('600519', force=True)
        self.assertTrue(d['available'])
        self.assertEqual(d['rz'], 173.0)  # 元 → 亿
        self.assertIsNone(d['rq'])  # 沪市明细无融券余额列
        self.assertEqual(d['chg_1d'], 3.0)  # 173 - 170
        self.assertAlmostEqual(d['chg_pct_1d'], 1.76, places=2)

    def test_szse_path_with_rq(self):
        def fake_sz(date):
            rz = 46.4e8 if date == '20260904' else 46.0e8
            return self._sz_df(rz, 0.88e8, rz + 0.88e8)

        with patch('stocks.market.margin_stock._recent_day_candidates',
                   return_value=[date(2026, 9, 4)]), \
             patch('stocks.market.margin_stock._prev_trading_day',
                   return_value=date(2026, 9, 3)), \
             patch('akshare.stock_margin_detail_szse', side_effect=fake_sz):
            d = market.fetch_stock_margin('000001', force=True)
        self.assertTrue(d['available'])
        self.assertEqual(d['rz'], 46.4)
        self.assertEqual(d['rq'], 0.88)
        self.assertEqual(d['total'], 47.28)
        self.assertEqual(d['chg_1d'], 0.4)

    def test_non_a_share_degrades(self):
        d = market.fetch_stock_margin('860001', force=True)
        self.assertFalse(d['available'])
        self.assertIn('仅沪深', d['message'])

    def test_not_target_degrades(self):
        with patch('stocks.market.margin_stock._recent_day_candidates',
                   return_value=[date(2026, 9, 4)]), \
             patch('akshare.stock_margin_detail_sse',
                   return_value=pd.DataFrame(columns=['标的证券代码', '融资余额', '标的证券简称'])):
            d = market.fetch_stock_margin('600519', force=True)
        self.assertFalse(d['available'])
        self.assertTrue(d['message'])


class HolderStructureTests(SimpleTestCase):
    """ETF 持有人结构（定报间接口径）：% 清洗与上游降级。"""

    def setUp(self):
        market._cache.clear()

    @staticmethod
    def _fixture():
        return (
            "var apidata={ content:\"<table><thead><tr><th>公告日期</th><th>机构持有比例</th>"
            "<th>个人持有比例</th><th>内部持有比例</th><th>总份额（亿份）</th></tr></thead><tbody>"
            "<tr><td>2026-06-30</td><td class='tor'>61.65%</td><td class='tor'>32.95%</td>"
            "<td class='tor'>5.40%</td><td class='tor'>189.15</td></tr>"
            "<tr><td>2025-12-31</td><td class='tor'>90.27%</td><td class='tor'>8.98%</td>"
            "<td class='tor'>0.75%</td><td class='tor'>888.30</td></tr>"
            "</tbody></table>\"};"
        )

    def test_parses_and_strips_pct(self):
        resp = type('R', (), {
            'text': self._fixture(), 'raise_for_status': lambda self: None,
        })()
        with patch('stocks.market.holders.requests.get', return_value=resp):
            d = market.holders.fetch_holder_structure('510300', force=True)
        self.assertTrue(d['available'])
        self.assertEqual(len(d['items']), 2)
        self.assertEqual(d['items'][0]['institution_pct'], 61.65)  # % 已清洗
        self.assertAlmostEqual(d['items'][0]['total_shares'], 189.15e8)  # 亿份→份（DAT-20）
        self.assertIn('间接观察', d['meta']['disclaimer'])

    def test_upstream_fail_degrades(self):
        with patch('stocks.market.holders.requests.get', side_effect=RuntimeError('boom')):
            d = market.holders.fetch_holder_structure('510300', force=True)
        self.assertFalse(d['available'])
        self.assertTrue(d['message'])
