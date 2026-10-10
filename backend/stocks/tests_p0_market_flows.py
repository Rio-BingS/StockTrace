"""P0 回归：大盘资金流窗口口径（DAT-05）与快照回填单位/口径（DAT-06、DS-02）。"""

from datetime import date, timedelta
from io import StringIO
from unittest.mock import patch

import pandas as pd
from django.core.management import call_command
from django.test import TestCase

from . import market
from .market import _sources, flows, snapshots
from .market.periods import PeriodWindow
from .models import MarketDailySnapshot

_D1 = date(2026, 9, 3)
_D2 = date(2026, 9, 4)


def _kline(day, main, part=1.0):
    # fflow daykline 行：日期,主力,小,中,大,超大,占比x5,收盘,涨跌幅
    return f'{day.isoformat()},{main},{part},{part},{part},{part},0.1,0.1,0.1,0.1,0.1,3000.0,0.5'


def _snap(day, main, **extra):
    return MarketDailySnapshot.objects.create(
        kind=MarketDailySnapshot.KIND_MARKET_FF, trade_date=day,
        payload=[{'date': day.isoformat(), 'main_net': main, 'super_net': 1.0, 'large_net': 1.0,
                  'mid_net': 1.0, 'small_net': 1.0, **extra}],
    )


def _akshare_market_frame():
    """按 akshare stock_market_fund_flow 实际返回的 15 列构造（净额/净占比成对）。"""
    return pd.DataFrame([{
        '日期': _D1, '上证-收盘价': 3000.0, '上证-涨跌幅': 0.5, '深证-收盘价': 9000.0, '深证-涨跌幅': 0.3,
        '主力净流入-净额': -5.2e10, '主力净流入-净占比': -4.1,
        '超大单净流入-净额': -3.0e10, '超大单净流入-净占比': -2.3,
        '大单净流入-净额': -2.2e10, '大单净流入-净占比': -1.8,
        '中单净流入-净额': 1.0e10, '中单净流入-净占比': 0.8,
        '小单净流入-净额': 4.2e10, '小单净流入-净占比': 3.3,
    }])


class MarketFlowWindowScopeTests(TestCase):
    """DAT-05：窗口只用沪深同日齐全的合计；单边成功不得覆盖快照里的两市合计。"""

    def setUp(self):
        market._cache.clear()
        self.window = PeriodWindow('custom', _D1, _D2)

    def test_single_market_upstream_does_not_override_snapshot(self):
        _snap(_D2, 1000.0)

        def fake(secid):
            if secid == '0.399001':
                raise RuntimeError('sz down')
            return [_kline(_D2, 600.0)]  # 只有沪市：不能冒充两市

        with patch('stocks.market.etf_flow.fetch_flow_klines', side_effect=fake):
            data = market.get_market_fund_flow_window(self.window)
        self.assertEqual(data['summary']['total_main_net'], 1000.0)
        self.assertEqual(data['summary']['days'], 1)
        self.assertIn('快照', data['message'])

    def test_only_dates_present_in_both_markets_are_merged(self):
        def fake(secid):
            if secid == '1.000001':
                return [_kline(_D1, 100.0), _kline(_D2, 200.0)]
            return [_kline(_D2, 50.0)]  # 深市缺 D1

        with patch('stocks.market.etf_flow.fetch_flow_klines', side_effect=fake):
            data = market.get_market_fund_flow_window(self.window)
        self.assertEqual([r['date'] for r in data['items']], [_D2.isoformat()])
        self.assertEqual(data['summary']['total_main_net'], 250.0)
        self.assertEqual(data['summary']['total_small_net'], 2.0)
        self.assertEqual(data['scope'], 'sh+sz')
        self.assertEqual(data['unit'], '元')
        self.assertIn('沪深合计', data['meta']['source'])

    def test_snapshot_and_window_use_same_merge(self):
        rows = snapshots.merge_market_ff_rows({
            '1.000001': {'2026-09-04': {'main_net': 1.0, 'small_net': None}},
            '0.399001': {'2026-09-04': {'main_net': 2.0, 'small_net': None}},
        })
        self.assertEqual(rows['2026-09-04']['main_net'], 3.0)
        self.assertIsNone(rows['2026-09-04']['small_net'])  # 两边都缺不拿 0 补
        self.assertEqual(snapshots.merge_market_ff_rows({'1.000001': {'2026-09-04': {}}}), {})


class MarketFundHistNormalizeTests(TestCase):
    """DS-02 列名撞名 + DAT-06 北向兜底源单位/口径。"""

    def setUp(self):
        market._cache.clear()
        _sources._source_state.clear()

    def test_real_columns_keep_all_parts(self):
        data = flows._normalize_market_fund_hist(_akshare_market_frame(), 30, flows.MARKET_FUND_SOURCE_EM)
        item = data['items'][0]
        self.assertEqual(item['main_net'], -5.2e10)  # 不是 -4.1（净占比）
        self.assertEqual(item['super_net'], -3.0e10)
        self.assertEqual(item['large_net'], -2.2e10)
        self.assertEqual(item['mid_net'], 1.0e10)
        self.assertEqual(item['small_net'], 4.2e10)
        self.assertTrue(data['available'])
        self.assertEqual((data['unit'], data['scope']), ('元', 'sh+sz'))

    def test_north_fallback_never_written_as_main_net(self):
        df = pd.DataFrame({'日期': ['2026-09-03'], '当日成交净买额': [52.3], '当日资金流入': [60.0]})
        data = flows._normalize_market_fund_hist(df, 30, flows.MARKET_FUND_SOURCE_NORTH)
        item = data['items'][0]
        self.assertIsNone(item['main_net'])
        self.assertAlmostEqual(item['north_net_buy'], 52.3e8)  # 亿元 -> 元
        self.assertFalse(data['available'])
        self.assertEqual(data['scope'], 'north')

    @patch('stocks.market.flows.ak.stock_hsgt_hist_em')
    @patch('stocks.market.flows.ak.stock_market_fund_flow', side_effect=RuntimeError('502'))
    def test_fetch_hist_north_fallback_is_unavailable(self, _em, mock_north):
        mock_north.return_value = pd.DataFrame({'日期': ['2026-09-03'], '当日成交净买额': [52.3]})
        data = flows.fetch_market_fund_flow_hist(days=30, force=True)
        self.assertEqual(data['source'], flows.MARKET_FUND_SOURCE_NORTH)
        self.assertFalse(data['available'])
        self.assertIsNone(data['items'][0]['main_net'])


@patch('stocks.market.snapshots._expected_latest_date', return_value=_D2)
class BackfillMarketFfTests(TestCase):
    """DAT-06：回填只认东财主源、只补缺不覆盖（除非 --force）、不写未收盘日期。"""

    def _hist(self, source, items):
        return patch('stocks.market.flows.fetch_market_fund_flow_hist',
                     return_value={'available': True, 'source': source, 'items': items, 'message': ''})

    @staticmethod
    def _item(day, main):
        return {'date': day.isoformat(), 'main_net': main, 'small_net': 1.0, 'mid_net': 1.0,
                'large_net': 1.0, 'super_net': 1.0}

    def _run(self, *args):
        out, err = StringIO(), StringIO()
        call_command('backfill_market_snapshots', *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_rejects_north_source(self, _latest):
        with self._hist(flows.MARKET_FUND_SOURCE_NORTH, [self._item(_D1, 52.3)]):
            _, err = self._run()
        self.assertIn('拒绝写入', err)
        self.assertFalse(MarketDailySnapshot.objects.filter(kind=MarketDailySnapshot.KIND_MARKET_FF).exists())

    def test_fills_missing_only_and_skips_unfinished_day(self, _latest):
        _snap(_D1, 9.9e10)
        future = _D2 + timedelta(days=1)
        with self._hist(flows.MARKET_FUND_SOURCE_EM,
                        [self._item(_D1, 1.0e10), self._item(_D2, 2.0e10), self._item(future, 3.0e10)]):
            self._run()
        rows = {s.trade_date: s.payload[0] for s in MarketDailySnapshot.objects.filter(
            kind=MarketDailySnapshot.KIND_MARKET_FF)}
        self.assertEqual(rows[_D1]['main_net'], 9.9e10)  # 收盘任务写的行不被覆盖
        self.assertEqual(rows[_D2]['main_net'], 2.0e10)
        self.assertEqual(rows[_D2]['src'], flows.MARKET_FUND_SOURCE_EM)
        self.assertEqual(rows[_D2]['unit'], '元')
        self.assertNotIn(future, rows)

    def test_force_overwrites(self, _latest):
        _snap(_D1, 9.9e10)
        with self._hist(flows.MARKET_FUND_SOURCE_EM, [self._item(_D1, 1.0e10)]):
            self._run('--force')
        row = MarketDailySnapshot.objects.get(kind=MarketDailySnapshot.KIND_MARKET_FF, trade_date=_D1)
        self.assertEqual(row.payload[0]['main_net'], 1.0e10)

    def test_industry_backfill_disabled(self, _latest):
        with self._hist(flows.MARKET_FUND_SOURCE_EM, []), \
                patch('stocks.market.sectors.fetch_industry_fund_flow') as mock_ind:
            self._run()
        mock_ind.assert_not_called()
        self.assertFalse(MarketDailySnapshot.objects.filter(kind=MarketDailySnapshot.KIND_INDUSTRY_FF).exists())


class AuditMarketFfTests(TestCase):
    """DAT-06：巡检疑似污染行，默认只报告不删。"""

    def setUp(self):
        self.good = _snap(_D2, -5.2e10)
        self.bad = _snap(_D1, 52.3)  # 亿元口径误写

    def test_audit_reports_only_by_default(self):
        suspects = snapshots.audit_market_ff_snapshots()
        self.assertEqual([s['trade_date'] for s in suspects], [_D1])
        self.assertIn('magnitude', suspects[0]['reasons'])
        out = StringIO()
        call_command('backfill_market_snapshots', '--audit', stdout=out, stderr=StringIO())
        self.assertIn('疑似异常 1 行', out.getvalue())
        self.assertEqual(MarketDailySnapshot.objects.filter(kind=MarketDailySnapshot.KIND_MARKET_FF).count(), 2)

    def test_audit_delete_suspect(self):
        call_command('backfill_market_snapshots', '--audit', '--delete-suspect',
                     stdout=StringIO(), stderr=StringIO())
        self.assertTrue(MarketDailySnapshot.objects.filter(pk=self.good.pk).exists())
        self.assertFalse(MarketDailySnapshot.objects.filter(pk=self.bad.pk).exists())

    def test_parts_all_none_flagged_but_not_deleted(self):
        MarketDailySnapshot.objects.filter(pk=self.good.pk).update(payload=[{
            'date': _D2.isoformat(), 'main_net': -5.2e10, 'super_net': None, 'large_net': None,
            'mid_net': None, 'small_net': None,
        }])
        reasons = {s['id']: s['reasons'] for s in snapshots.audit_market_ff_snapshots()}
        self.assertEqual(reasons[self.good.pk], ['parts_all_none'])
        call_command('backfill_market_snapshots', '--audit', '--delete-suspect',
                     stdout=StringIO(), stderr=StringIO())
        self.assertTrue(MarketDailySnapshot.objects.filter(pk=self.good.pk).exists())
