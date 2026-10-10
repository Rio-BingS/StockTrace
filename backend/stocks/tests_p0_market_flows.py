"""P0 回归：大盘资金流窗口口径（DAT-05）与快照回填单位/口径（DAT-06、DS-02）。"""

from datetime import date
from unittest.mock import patch

import pandas as pd
from django.test import TestCase

from . import market
from .market import snapshots
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
