import socket
from datetime import date, datetime
from time import monotonic, time
from unittest.mock import patch

import pandas as pd
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from . import market
from . import tasks
from .market._cache import _cache_get, _cache_set


def _no_holders(code, **kwargs):
    """持有人结构是独立上游（东财基金档案），ETF 详情用例不关心：固定为不可用，避免触网（OPS-03）"""
    return {'available': False, 'items': [], 'message': '测试桩：不拉持有人结构', 'meta': {}}


class MarketParsingTests(SimpleTestCase):
    def setUp(self):
        market._cache.clear()

    def test_etf_normalization_and_scope(self):
        equity = market._etf_row_to_item(pd.Series({
            '代码': '510300', '名称': '沪深300ETF', '最新价': 4.2,
            '最新份额': 100000000, '成交额': 200000000,
            '总市值': 420000000, '主力净流入-净额': 1200000,
            'IOPV实时估值': 4.19, '昨收': 4.1,
        }))
        bond = market._etf_row_to_item(pd.Series({'代码': '511010', '名称': '国债ETF'}))

        self.assertTrue(equity['scope_match'])
        self.assertEqual(equity['exchange'], 'SH')
        self.assertEqual(equity['iopv'], 4.19)
        self.assertFalse(bond['scope_match'])

    def test_sector_amounts_are_converted_from_yi_to_yuan(self):
        frame = pd.DataFrame([{
            '行业': '科技', '净额': 12.5, '流入资金': 20, '流出资金': 7.5,
            '行业-涨跌幅': 2.1, '领涨股': '示例', '领涨股-涨跌幅': 8.2,
        }])
        item = market._parse_fund_flow_table(frame, ['行业'])[0]
        self.assertEqual(item['net'], 1_250_000_000)
        self.assertEqual(item['inflow'], 2_000_000_000)
        self.assertEqual(item['outflow'], 750_000_000)

    def test_sector_parser_deduplicates_names_by_stronger_net_amount(self):
        frame = pd.DataFrame([
            {'行业': 'AI', '净额': 10, '流入资金': 20, '流出资金': 10},
            {'行业': 'AI', '净额': -15, '流入资金': 5, '流出资金': 20},
            {'行业': '算力', '净额': 8, '流入资金': 10, '流出资金': 2},
        ])
        items = market._parse_fund_flow_table(frame, ['行业'])

        self.assertEqual(len(items), 2)
        self.assertEqual(next(item for item in items if item['name'] == 'AI')['net'], -1_500_000_000)

    @patch('stocks.market.etf._fetch_etf_spot_df')
    def test_etf_radar_filters_sorts_and_paginates(self, fetch_spot):
        fetch_spot.return_value = pd.DataFrame([
            {'代码': '510300', '名称': '沪深300ETF', '最新份额': 300, '成交额': 1000, '主力净流入-净额': 10},
            {'代码': '159915', '名称': '创业板ETF', '最新份额': 200, '成交额': 2000, '主力净流入-净额': -5},
            {'代码': '511010', '名称': '国债ETF', '最新份额': 400, '成交额': 3000, '主力净流入-净额': 1},
        ])
        data = market.get_etf_share_radar(scope='equity_broad', rank='turnover', page=1, page_size=1)

        self.assertEqual(data['scope']['all_count'], 3)
        self.assertEqual(data['scope']['equity_broad_count'], 2)
        self.assertEqual(data['pagination']['total'], 2)
        self.assertEqual(data['items'][0]['code'], '159915')
        self.assertFalse(data['supported_metrics']['share_change_5d'])

    @patch('stocks.market.etf._fetch_etf_spot_df')
    def test_etf_radar_summary_uses_filtered_full_result_and_sort_is_stable(self, fetch_spot):
        fetch_spot.return_value = pd.DataFrame([
            {'代码': '510500', '名称': '中证500ETF', '成交额': 2000, '主力净流入-净额': 20},
            {'代码': '510300', '名称': '沪深300ETF', '成交额': 2000, '主力净流入-净额': 10},
            {'代码': '159915', '名称': '创业板ETF', '成交额': 500, '主力净流入-净额': -5},
        ])
        data = market.get_etf_share_radar(
            scope='equity_broad', rank='turnover', min_turnover=1000,
            page=1, page_size=1,
        )

        self.assertEqual(data['pagination']['total'], 2)
        self.assertEqual(data['summary']['count'], 2)
        self.assertEqual(data['summary']['total_turnover'], 4000)
        self.assertEqual(data['summary']['total_main_net'], 30)
        self.assertEqual(data['items'][0]['code'], '510300')

    def test_sector_payload_has_current_day_analytics(self):
        items = [
            {'name': 'A', 'net': 100, 'inflow': 120, 'outflow': 20, 'change_pct': 1, 'leader': 'A1', 'leader_pct': 3},
            {'name': 'B', 'net': 50, 'inflow': 70, 'outflow': 20, 'change_pct': -1, 'leader': 'B1', 'leader_pct': 2},
            {'name': 'C', 'net': -80, 'inflow': 20, 'outflow': 100, 'change_pct': 1, 'leader': 'C1', 'leader_pct': 2},
        ]
        data = market._sector_payload(items, 'industry')

        self.assertEqual(data['summary']['sample_count'], 3)
        self.assertEqual(data['summary']['inflow_count'], 2)
        self.assertEqual(data['summary']['outflow_count'], 1)
        self.assertEqual(data['summary']['neutral_count'], 0)
        self.assertEqual(data['period'], 'day')
        self.assertEqual(data['unavailable_periods'], ['1m', '3m', '6m', '1y'])
        self.assertEqual([item['name'] for item in data['divergences']], ['C', 'B'])

    def test_sector_rank_table_uses_upstream_yuan_values(self):
        # 东财 clist 排行榜形状：列带指标前缀，净额单位已是元
        frame = pd.DataFrame([
            {'序号': 1, '行业': '半导体', '5日涨跌幅': 2.5, '5日主力净流入-净额': 1_500_000_000.0},
            {'序号': 2, '行业': '软件', '5日涨跌幅': -1.2, '5日主力净流入-净额': -800_000_000.0},
        ])
        items = market._parse_sector_rank_table(frame)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['net'], 1_500_000_000.0)
        self.assertEqual(items[1]['change_pct'], -1.2)
        self.assertIsNone(items[0]['inflow'])

    @patch('stocks.market.sectors.fetch_industry_fund_flow')
    def test_sector_rotation_multi_day_periods_use_snapshots_only(self, fetch_flow):
        # 多日口径固定为日度快照（DAT-15），不再调用东财原生 5日排行
        data = market.get_sector_rotation(board='industry', period='5d')

        fetch_flow.assert_not_called()
        self.assertEqual(data['period'], '5d')
        self.assertEqual(data['basis'], 'ths_snapshot')
        self.assertIn('5d', data['supported_periods'])

    @patch('stocks.market.etf_flow.flow_history')
    @patch('stocks.market.etf_flow._FLOW_FETCH_INTERVAL', 0)
    def test_flow_trading_day_periods_take_recent_rows(self, mock_hist):
        from datetime import date, timedelta

        base = {'small_net': None, 'mid_net': None, 'large_net': None, 'super_net': None,
                'close': 1.0, 'change_pct': 0.0}
        rows = [
            {'date': (date.today() - timedelta(days=n)).isoformat(), 'main_net': float(n), **base}
            for n in range(9, 0, -1)
        ]
        mock_hist.return_value = rows
        data = market.get_national_team_flow(period='3d')

        top = data['items'][0]
        self.assertEqual(top['days'], 3)
        # 尾部 3 行的 main_net：3 + 2 + 1
        self.assertEqual(top['total_main_net'], 6.0)

    def test_flow_custom_range_validation(self):
        with self.assertRaises(ValueError):
            market.get_national_team_flow(start='2026-08-10', end='2026-08-01')
        with self.assertRaises(ValueError):
            market.get_national_team_flow(start='2026/08/01')

    @patch('stocks.market.etf._fetch_etf_spot_df')
    def test_national_etf_is_explicitly_watchlist_not_holdings(self, fetch_spot):
        fetch_spot.return_value = pd.DataFrame([{
            '代码': '510300', '名称': '沪深300ETF', '最新价': 4.2,
            '最新份额': 100, '总市值': 420, '数据日期': '2026-07-31',
        }])

        data = market.get_national_team_etfs()

        self.assertEqual(data['mode'], 'watchlist')
        self.assertFalse(data['official_disclosure_available'])
        self.assertFalse(data['watchlist_definition']['is_official_holding'])
        self.assertEqual(data['meta']['source_data_date'], '2026-07-31')
        forbidden = {'holder', 'weight', 'holding_shares', 'holding_cost', 'institution'}
        for item in data['items']:
            self.assertTrue(forbidden.isdisjoint(item))

    @patch('stocks.market.etf.fetch_holder_structure', new=_no_holders)
    @patch('stocks.market.etf._etf_history')
    @patch('stocks.market.etf._normalized_etf_items')
    def test_etf_detail_returns_price_history_only(self, normalized, history):
        normalized.return_value = [{
            'code': '510300', 'name': '沪深300ETF', 'exchange': 'SH',
            'share': 100, 'data_date': '2026-07-31',
        }]
        history.return_value = [
            {'date': f'2026-01-{i:02d}', 'close': float(i)} for i in range(1, 22)
        ]
        data = market.get_etf_detail('510300', '3m')

        self.assertEqual(data['share_metrics']['availability'], 'latest_only')
        self.assertIsNotNone(data['price_performance']['return_5d'])
        self.assertEqual(data['history']['interval'], '1d')
        self.assertTrue(data['history']['available'])
        self.assertEqual(data['history']['count'], 21)
        self.assertEqual(data['history']['start_date'], '2026-01-01')
        self.assertEqual(data['history']['end_date'], '2026-01-21')

    @patch('stocks.market.etf.fetch_holder_structure', new=_no_holders)
    @patch('stocks.market.etf._etf_history', return_value=[])
    @patch('stocks.market.etf._normalized_etf_items')
    def test_etf_detail_keeps_quote_when_history_is_unavailable(self, normalized, history):
        normalized.return_value = [{
            'code': '510300', 'name': '沪深300ETF', 'exchange': 'SH',
            'share': 100, 'data_date': '2026-07-31',
        }]

        data = market.get_etf_detail('510300', '3m')

        self.assertTrue(data['meta']['available'])
        self.assertFalse(data['history']['available'])
        self.assertEqual(data['history']['meta']['cache_status'], 'unavailable')
        self.assertIsNone(data['history']['start_date'])
        self.assertIsNone(data['price_performance']['return_5d'])


class MarketCacheFreshnessTests(SimpleTestCase):
    """交易日历感知的新鲜度：TTL 过期但已覆盖最近收盘时，非交易时段不重拉。"""

    def test_stale_entry_fresh_when_covers_last_session_close(self):
        close = timezone.make_aware(datetime(2026, 8, 28, 15, 0))
        with patch('stocks.market._cache._calendar_enabled', return_value=True), \
             patch('stocks.market._cache._data_changing_now', return_value=False), \
             patch('stocks.market._cache._last_session_close', return_value=close):
            self.assertTrue(market._is_fresh(close.timestamp() + 60, ttl=60))
            self.assertFalse(market._is_fresh(close.timestamp() - 60, ttl=60))

    def test_ttl_applies_while_data_changing(self):
        with patch('stocks.market._cache._calendar_enabled', return_value=True), \
             patch('stocks.market._cache._data_changing_now', return_value=True):
            self.assertFalse(market._is_fresh(time() - 600, ttl=60))
            self.assertTrue(market._is_fresh(time() - 10, ttl=60))


class NationalEtfFlowTests(SimpleTestCase):
    """国家队 ETF 区间资金流：ETF 行解析、窗口聚合与缓存。"""

    def setUp(self):
        market._cache.clear()

    def test_parse_kline_etf_schema(self):
        # 东财 ETF 资金流行：日期,主力,小单,中单,大单,超大单,各占比,收盘,涨跌幅
        row = ('2026-08-28,244913792.0,-66497392.0,-178416395.0,-124100400.0,'
               '369014192.0,8.64,-2.35,-6.29,-4.38,13.02,4.679,-0.26,0.00,0.00')
        parsed = market.etf_flow._parse_kline(row)

        self.assertEqual(parsed['date'], '2026-08-28')
        self.assertEqual(parsed['main_net'], 244913792.0)
        # 主力 = 超大单 + 大单
        self.assertAlmostEqual(parsed['main_net'], parsed['super_net'] + parsed['large_net'], places=5)
        self.assertEqual(parsed['close'], 4.679)
        self.assertEqual(parsed['change_pct'], -0.26)

    def test_short_kline_row_is_skipped(self):
        self.assertIsNone(market.etf_flow._parse_kline('2026-08-28,1.0,2.0'))

    @patch('stocks.market.etf_flow._FLOW_FETCH_INTERVAL', 0)
    @patch('stocks.market.etf_flow.flow_history')
    def test_flow_aggregation_sums_window_and_caches(self, mock_hist):
        from datetime import date, timedelta

        d1 = (date.today() - timedelta(days=1)).isoformat()
        d2 = date.today().isoformat()

        def rows_for(main_net):
            base = {'small_net': None, 'mid_net': None, 'large_net': None, 'super_net': None}
            return [
                {'date': d1, 'main_net': 100.0, 'close': 1.0, 'change_pct': 0.0, **base},
                {'date': d2, 'main_net': main_net, 'close': 1.1, 'change_pct': 1.0, **base},
            ]

        mock_hist.side_effect = lambda code: rows_for(1_000_000.0 if code == '510050' else -500_000.0)
        data = market.get_national_team_flow(period='1w')

        self.assertTrue(data['available'])
        self.assertEqual(data['end'], d2)
        top = data['items'][0]
        self.assertEqual(top['code'], '510050')
        self.assertEqual(top['total_main_net'], 1_000_100.0)
        self.assertEqual(data['summary']['inflow_count'], 1)
        self.assertEqual(data['summary']['outflow_count'], 17)
        self.assertEqual(data['total_daily'][-1]['main_net'], -7_500_000.0)

        calls = mock_hist.call_count
        market.get_national_team_flow(period='1w')
        self.assertEqual(mock_hist.call_count, calls)  # 第二次命中进程缓存


class PeriodWindowTests(SimpleTestCase):
    """统一区间解析：预设档位窗口、自定义起止与校验。"""

    def _resolve(self, params, **kwargs):
        from stocks.market.periods import FULL_PRESETS, resolve_period

        merged = {'period': '', 'start': '', 'end': ''}
        merged.update(params)
        return resolve_period(
            lambda key, default='': merged.get(key, default),
            kwargs.pop('allowed', FULL_PRESETS), today=date(2026, 8, 30), **kwargs,
        )

    def test_preset_window_uses_natural_days(self):
        from datetime import date

        window = self._resolve({'period': '3m'})
        self.assertEqual(window.start, date(2026, 6, 2))
        self.assertEqual(window.end, date(2026, 8, 30))

    def test_1d_window_is_single_day(self):
        window = self._resolve({'period': '1d'})
        self.assertEqual(window.start, date(2026, 8, 30))
        self.assertEqual(window.end, date(2026, 8, 30))

    def test_custom_window(self):
        window = self._resolve({'period': 'custom', 'start': '2026-03-01', 'end': '2026-03-31'})
        self.assertEqual(window.preset, 'custom')
        self.assertEqual(window.start.isoformat(), '2026-03-01')

    def test_custom_start_after_end_raises(self):
        with self.assertRaises(ValueError):
            self._resolve({'period': 'custom', 'start': '2026-03-31', 'end': '2026-03-01'})

    def test_custom_span_over_limit_raises(self):
        with self.assertRaises(ValueError):
            self._resolve({'period': 'custom', 'start': '2020-01-01', 'end': '2026-03-01'})

    def test_disallowed_preset_raises(self):
        with self.assertRaises(ValueError):
            self._resolve({'period': '2w'}, allowed=['1d', '1w'])

    def test_custom_not_allowed_raises(self):
        with self.assertRaises(ValueError):
            self._resolve(
                {'period': 'custom', 'start': '2026-03-01', 'end': '2026-03-31'},
                allow_custom=False,
            )


class MarketWindowEndpointTests(SimpleTestCase):
    """大盘资金流/北向窗口聚合端点。"""

    def setUp(self):
        market._cache.clear()

    @patch('stocks.market.etf_flow.fetch_flow_klines')
    def test_market_flow_window_sums_and_drops_out_of_range(self, mock_fetch):
        from stocks.market.periods import PeriodWindow
        from datetime import date

        window = PeriodWindow('custom', date(2026, 8, 27), date(2026, 8, 28))
        sh = [
            '2026-08-26,100.0,0.0,0.0,0.0,0.0,0,0,0,0,0,3000.0,0.5',   # 窗口外
            '2026-08-27,200.0,0.0,0.0,0.0,0.0,0,0,0,0,0,3100.0,0.5',
            '2026-08-28,-50.0,0.0,0.0,0.0,0.0,0,0,0,0,0,3090.0,-0.1',
        ]
        sz = [
            '2026-08-26,10.0,0.0,0.0,0.0,0.0,0,0,0,0,0,9000.0,0.5',
            '2026-08-27,20.0,0.0,0.0,0.0,0.0,0,0,0,0,0,9100.0,0.5',
            '2026-08-28,-5.0,0.0,0.0,0.0,0.0,0,0,0,0,0,9090.0,-0.1',
        ]
        mock_fetch.side_effect = lambda secid: sh if secid == '1.000001' else sz
        data = market.get_market_fund_flow_window(window)

        self.assertTrue(data['available'])
        self.assertEqual(data['summary']['days'], 2)
        # 沪深同日合计（DAT-05）：(200+20) + (-50-5)
        self.assertEqual(data['summary']['total_main_net'], 165.0)
        self.assertEqual(data['scope'], 'sh+sz')
        self.assertEqual(data['summary']['inflow_days'], 1)
        self.assertEqual(data['coverage_start'], '2026-08-26')  # 上游覆盖早于窗口起点
        self.assertFalse(data['truncated'])  # 覆盖范围完全包含窗口，未截断

    @patch('stocks.market.institutions.fetch_northbound_flow_series')
    def test_northbound_window_slices_series(self, mock_series):
        from stocks.market.periods import PeriodWindow
        from datetime import date

        window = PeriodWindow('custom', date(2026, 8, 27), date(2026, 8, 28))
        mock_series.return_value = {
            'source': 'stock_hsgt_hist_em',
            'items': [
                {'date': '2026-08-26', 'net_buy': 10.0},
                {'date': '2026-08-27', 'net_buy': 20.0},
                {'date': '2026-08-28', 'net_buy': -5.0},
            ],
        }
        data = market.get_northbound_window(window)

        self.assertTrue(data['available'])
        self.assertEqual(data['summary']['days'], 2)
        self.assertEqual(data['summary']['total_net_buy'], 15.0)

    @patch('stocks.market.institutions.fetch_northbound_flow_series')
    def test_northbound_window_all_null_net_buy_is_unavailable(self, mock_series):
        # 2024-08 披露调整后上游不再提供净买额：全 null 时如实标不可用，不得返回 0
        from stocks.market.periods import PeriodWindow
        from datetime import date

        window = PeriodWindow('custom', date(2026, 9, 1), date(2026, 9, 2))
        mock_series.return_value = {
            'source': 'stock_hsgt_hist_em',
            'items': [
                {'date': '2026-09-01', 'net_buy': None, 'inflow': None},
                {'date': '2026-09-02', 'net_buy': None, 'inflow': None},
            ],
        }
        data = market.get_northbound_window(window)

        self.assertFalse(data['available'])
        self.assertIsNone(data['summary']['total_net_buy'])
        self.assertEqual(data['summary']['days'], 0)
        self.assertIn('披露', data['message'])

    def test_normalize_market_fund_hist_all_null_main_net_returns_none(self):
        # 北向兜底序列整列为空时视为无数据，不得以 available=True 空画图
        df = pd.DataFrame({
            '日期': ['2026-09-01', '2026-09-02'],
            '当日成交净买额': [None, None],
        })
        from stocks.market.flows import _normalize_market_fund_hist

        self.assertIsNone(_normalize_market_fund_hist(df, 30, 'eastmoney_hsgt_hist_north'))

    @patch('stocks.market.flows.ak.stock_hsgt_fund_flow_summary_em')
    def test_hsgt_north_zero_placeholder_becomes_null(self, mock_src):
        # 北向净买额 0 为上游占位（披露停止），应转 null；南向真实值保留
        mock_src.return_value = pd.DataFrame([
            {'交易日': '2026-09-04', '类型': '沪港通', '板块': '沪股通', '资金方向': '北向',
             '交易状态': 3, '成交净买额': 0.0, '资金净流入': 0.0, '当日资金余额': 0.0,
             '上涨数': 735, '持平数': 63, '下跌数': 845, '相关指数': '上证指数', '指数涨跌幅': -0.3},
            {'交易日': '2026-09-04', '类型': '沪港通', '板块': '港股通(沪)', '资金方向': '南向',
             '交易状态': 3, '成交净买额': -47.08, '资金净流入': 420.0, '当日资金余额': 0.0,
             '上涨数': 446, '持平数': 13, '下跌数': 162, '相关指数': '恒生指数', '指数涨跌幅': 1.74},
        ])
        rows = market.fetch_hsgt_flow(force=True)

        north = next(r for r in rows if r['direction'] == '北向')
        south = next(r for r in rows if r['direction'] == '南向')
        self.assertIsNone(north['net_buy'])
        self.assertIsNone(north['net_inflow'])
        self.assertEqual(south['net_buy'], -47.08)
        self.assertEqual(south['net_inflow'], 420.0)


class MarketApiValidationTests(SimpleTestCase):
    def test_invalid_etf_scope_returns_400(self):
        response = self.client.get('/api/market/etf-radar/?scope=bad')
        self.assertEqual(response.status_code, 400)

    def test_invalid_etf_query_parameters_return_400(self):
        for query in (
            'rank=unknown', 'sort=raw_dataframe_column', 'order=sideways',
            'page=0', 'page=abc', 'min_turnover=-1', 'min_turnover=abc',
        ):
            with self.subTest(query=query):
                response = self.client.get(f'/api/market/etf-radar/?{query}')
                self.assertEqual(response.status_code, 400)

    def test_invalid_sector_query_parameters_return_400(self):
        for query in ('board=bad', 'period=bad', 'sort=bad', 'order=bad', 'page_size=0'):
            with self.subTest(query=query):
                response = self.client.get(f'/api/market/sectors/?{query}')
                self.assertEqual(response.status_code, 400)

    def test_invalid_trend_period_returns_400(self):
        response = self.client.get('/api/market/trend/?period=bad')
        self.assertEqual(response.status_code, 400)

    def test_invalid_trend_custom_range_returns_400(self):
        response = self.client.get('/api/market/trend/?start=2026-08-10&end=2026-08-01')
        self.assertEqual(response.status_code, 400)

    def test_invalid_national_flow_custom_range_returns_400(self):
        response = self.client.get('/api/market/national-etf/flow/?start=2026-08-10&end=2026-08-01')
        self.assertEqual(response.status_code, 400)

    @patch('stocks.market.etf._fetch_etf_spot_df')
    def test_etf_radar_api_accepts_filters_and_caps_page_size(self, fetch_spot):
        fetch_spot.return_value = pd.DataFrame([
            {'代码': '510300', '名称': '沪深300ETF', '成交额': 2_000_000_000, '最新份额': 100},
            {'代码': '159915', '名称': '创业板ETF', '成交额': 500_000_000, '最新份额': 80},
        ])
        response = self.client.get(
            '/api/market/etf-radar/?scope=equity_broad&rank=turnover&q=300&'
            'min_turnover=1000000000&page=1&page_size=999'
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['pagination']['page_size'], 100)
        self.assertEqual(response.data['pagination']['total'], 1)
        self.assertEqual(response.data['items'][0]['code'], '510300')

    def test_invalid_etf_code_returns_400(self):
        response = self.client.get('/api/market/etfs/abc/')
        self.assertEqual(response.status_code, 400)

    def test_invalid_etf_range_returns_400(self):
        response = self.client.get('/api/market/etfs/510300/?range=5y')
        self.assertEqual(response.status_code, 400)

    def test_invalid_trend_days_returns_400(self):
        response = self.client.get('/api/market/trend/?days=45')
        self.assertEqual(response.status_code, 400)

    def test_invalid_national_flow_period_returns_400(self):
        response = self.client.get('/api/market/national-etf/flow/?period=bad')
        self.assertEqual(response.status_code, 400)

    def test_invalid_sector_page_returns_400(self):
        response = self.client.get('/api/market/sectors/?page=0')
        self.assertEqual(response.status_code, 400)


class FetchTaskWatchdogTests(SimpleTestCase):
    """后台拉取任务看门狗：挂死线程不得永久阻塞新任务。"""

    def setUp(self):
        with tasks._lock:
            self._orig_state = dict(tasks._state)
            self._orig_started_at = tasks._started_at

    def tearDown(self):
        with tasks._lock:
            tasks._state.clear()
            tasks._state.update(self._orig_state)
            tasks._started_at = self._orig_started_at

    @staticmethod
    def _claim_running(task):
        with tasks._lock:
            tasks._state.update(running=True, task=task, last_status='running')
            tasks._started_at = monotonic()

    def test_get_fetch_status_reports_running_seconds(self):
        self._claim_running('one')
        with tasks._lock:
            tasks._started_at = monotonic() - 65
        st = tasks.get_fetch_status()
        self.assertTrue(st['running'])
        self.assertGreaterEqual(st['running_seconds'], 60)

    def test_fresh_running_task_not_recovered(self):
        self._claim_running('all')
        with tasks._lock:
            tasks._started_at = monotonic() - 60
            tasks._recover_stale_locked()
            self.assertTrue(tasks._state['running'])
            self.assertEqual(tasks._state['last_status'], 'running')

    def test_stale_running_task_only_marked(self):
        # REL-04：看门狗只标记 stale，不复位 running（旧线程仍持锁）
        self._claim_running('all')
        with tasks._lock:
            tasks._started_at = monotonic() - 31 * 60
            tasks._recover_stale_locked()
            self.assertTrue(tasks._state['running'])
            self.assertTrue(tasks._state['stale'])
            self.assertIn('疑似挂死', tasks._state['last_error'])

    def test_start_does_not_run_concurrently_with_stale_task(self):
        self._claim_running('all')
        with tasks._lock:
            tasks._started_at = monotonic() - 31 * 60
        with patch('stocks.tasks.threading.Thread') as thread_mock:
            started, st = tasks.start_fetch_one(1)
        self.assertFalse(started)
        self.assertTrue(st['stale'])
        thread_mock.assert_not_called()


class UpstreamTimeoutSettingsTests(SimpleTestCase):
    """settings 必须设进程级 socket 默认超时，防上游无响应挂死线程。"""

    def test_default_socket_timeout_is_set(self):
        self.assertIsNotNone(socket.getdefaulttimeout())
        self.assertGreater(socket.getdefaulttimeout(), 0)


class MarketCacheLruTests(SimpleTestCase):
    """进程内缓存必须有条目上限：部分 key 含用户可控输入，防内存无界增长。"""

    def setUp(self):
        market._cache.clear()

    def tearDown(self):
        market._cache.clear()

    def test_cache_evicts_oldest_beyond_limit(self):
        for i in range(257):
            _cache_set(f'k{i}', {'i': i})
        self.assertNotIn('k0', market._cache)
        self.assertIn('k256', market._cache)
        self.assertLessEqual(len(market._cache), 256)

    def test_rewritten_entry_survives_eviction(self):
        _cache_set('old', 1)
        for i in range(255):
            _cache_set(f'k{i}', i)
        _cache_set('old', 2)  # 重写视为最近使用
        _cache_set('k255', 3)  # 超限淘汰最旧的 k0 而非 old
        self.assertNotIn('k0', market._cache)
        self.assertEqual(_cache_get('old', ttl=60), 2)


class InstitutionCodeValidationTests(SimpleTestCase):
    """机构明细的 code 参数是用户可控输入，非法值必须 400 而非打到上游。"""

    def test_get_institution_holdings_rejects_bad_code(self):
        for bad in ('AB12', '12345', '1234567', '12 456', '../x'):
            with self.assertRaises(ValueError):
                market.get_institution_holdings(stock_code=bad)

    def test_fetch_stock_institution_detail_rejects_bad_code(self):
        for bad in ('AB12', '12345', '1234567'):
            with self.assertRaises(ValueError):
                market.fetch_stock_institution_detail(bad)


class TrendPeriodEndpointTests(SimpleTestCase):
    """走势端点各档位不应因视图层缺陷 500/502。"""

    @patch('stocks.views.get_market_trend', return_value={'series': []})
    def test_trend_ytd_period_no_longer_502(self, _trend_mock):
        response = self.client.get('/api/market/trend/?period=ytd')
        self.assertEqual(response.status_code, 200)


class StockEndpointDbTests(TestCase):
    """带数据库的日线/分钟端点行为。"""

    def setUp(self):
        from stocks.models import Stock
        self.stock = Stock.objects.create(code='600000', name='浦发银行')

    def test_invalid_daily_date_returns_400(self):
        response = self.client.get(f'/api/stocks/{self.stock.id}/daily/?date=2026-13-99')
        self.assertEqual(response.status_code, 400)

    def test_invalid_daily_days_returns_400(self):
        for bad in ('abc', '0', '-5'):
            with self.subTest(days=bad):
                response = self.client.get(f'/api/stocks/{self.stock.id}/daily/?days={bad}')
                self.assertEqual(response.status_code, 400)

    def test_invalid_minutes_date_returns_400(self):
        response = self.client.get(f'/api/stocks/{self.stock.id}/minutes/?date=bad')
        self.assertEqual(response.status_code, 400)

    def test_minutes_without_date_returns_latest_day_only(self):
        from datetime import datetime, timezone as tz
        from stocks.models import MinuteBar
        MinuteBar.objects.create(
            stock=self.stock, datetime=datetime(2026, 8, 29, 9, 31, tzinfo=tz.utc),
            open=1, close=1, high=1, low=1, volume=10,
        )
        MinuteBar.objects.create(
            stock=self.stock, datetime=datetime(2026, 8, 30, 9, 31, tzinfo=tz.utc),
            open=2, close=2, high=2, low=2, volume=20,
        )
        MinuteBar.objects.create(
            stock=self.stock, datetime=datetime(2026, 8, 30, 9, 32, tzinfo=tz.utc),
            open=3, close=3, high=3, low=3, volume=30,
        )
        response = self.client.get(f'/api/stocks/{self.stock.id}/minutes/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 2)  # 只返回最新一天（8-30 两根）

    def test_minutes_empty_stock_returns_empty_list(self):
        response = self.client.get(f'/api/stocks/{self.stock.id}/minutes/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])
