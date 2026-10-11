"""P2 数据方向修复回归测试（DAT-14~20、PERF-07、PERF-10、ARC-03、AIX-08）"""

from unittest.mock import patch

import pandas as pd
from django.test import SimpleTestCase

from . import market


def _no_holders(code, **kwargs):
    return {'available': False, 'items': [], 'message': '', 'meta': {}}


_QUOTE = [{
    'code': '510300', 'name': '沪深300ETF', 'exchange': 'SH',
    'share': 100, 'data_date': '2026-09-04',
}]


@patch('stocks.market.etf.fetch_holder_structure', new=_no_holders)
@patch('stocks.market.etf._normalized_etf_items', return_value=_QUOTE)
class EtfHistoryAdjustTests(SimpleTestCase):
    """DAT-14：东财取前复权；新浪兜底按 [start, end] 双边截断并如实标注不复权。"""

    def test_em_requests_qfq_and_marks_adjust(self, _items):
        em_df = pd.DataFrame([
            {'日期': '2026-08-03', '开盘': 4.0, '收盘': 4.1, '最高': 4.2, '最低': 3.9,
             '成交量': 1000, '成交额': 4100000, '涨跌幅': 1.2, '换手率': 0.5},
        ])
        with patch('akshare.fund_etf_hist_em', return_value=em_df) as em:
            data = market.get_etf_detail(
                '510300', 'custom', start_date='2026-08-01', end_date='2026-08-31')
        self.assertEqual(em.call_args.kwargs['adjust'], 'qfq')
        self.assertEqual(data['history']['adjust'], 'qfq')
        self.assertIn('前复权', data['history']['meta']['disclaimer'])

    def test_sina_fallback_respects_end_date(self, _items):
        sina_df = pd.DataFrame([
            {'date': d, 'open': 4.0, 'high': 4.1, 'low': 3.9, 'close': 4.0, 'volume': 1000}
            for d in ('2026-07-31', '2026-08-03', '2026-08-31', '2026-09-01', '2026-09-02')
        ])
        with patch('akshare.fund_etf_hist_em', side_effect=RuntimeError('502')), patch(
            'akshare.fund_etf_hist_sina', return_value=sina_df,
        ):
            data = market.get_etf_detail(
                '510300', 'custom', start_date='2026-08-01', end_date='2026-08-31')
        dates = [item['date'] for item in data['history']['items']]
        self.assertEqual(dates, ['2026-08-03', '2026-08-31'])
        self.assertEqual(data['history']['end_date'], '2026-08-31')
        self.assertEqual(data['history']['adjust'], 'none')
        self.assertIn('不复权', data['history']['meta']['disclaimer'])


class SectorBasisTests(SimpleTestCase):
    """DAT-15：当日 meta 如实标注同花顺自抓；多日口径固定为快照。"""

    @patch('stocks.market.sectors.fetch_concept_fund_flow', return_value=[
        {'name': '算力', 'net': 1.0, 'inflow': None, 'outflow': None, 'change_pct': 1.0,
         'index_value': None, 'company_count': None, 'leader': None, 'leader_pct': None},
    ])
    def test_day_meta_source_is_ths(self, _flow):
        data = market.get_sector_rotation(board='concept', period='day')
        self.assertEqual(data['basis'], 'ths_day')
        self.assertIn('10jqka.com.cn/funds/gnzjl', data['meta']['source'])
        self.assertNotIn('akshare', data['meta']['source'])

    def test_10d_non_net_sort_rejected(self):
        with self.assertRaisesMessage(ValueError, '仅支持按净额排序'):
            market.get_sector_rotation(board='industry', period='10d', sort='leader_pct')


class InstitutionQuarterTests(SimpleTestCase):
    """DAT-16：「最近季度」只选披露截止日已过的报告期。"""

    def test_latest_disclosed_quarter_by_deadline(self):
        from datetime import date

        from .market.institutions import _latest_disclosed_quarter, _recent_report_quarters

        cases = {
            date(2026, 10, 11): (2026, 2),   # 三季报 10/31 前仍在披露
            date(2026, 10, 31): (2026, 2),   # 截止日当天仍未完
            date(2026, 11, 1): (2026, 3),
            date(2026, 8, 31): (2026, 1),
            date(2026, 9, 1): (2026, 2),
            date(2026, 2, 15): (2025, 3),    # 年报 4/30 前
            date(2026, 4, 30): (2025, 3),
            date(2026, 5, 1): (2026, 1),     # 年报与一季报同日截止
        }
        for today, expected in cases.items():
            self.assertEqual(_latest_disclosed_quarter(today), expected, today)
        self.assertEqual(
            _recent_report_quarters(3, today=date(2026, 5, 1)), ['20261', '20254', '20253'],
        )

    def test_hold_stocks_skips_quarter_in_disclosure(self):
        from datetime import date

        df = pd.DataFrame([{'证券代码': '600000', '证券简称': '浦发银行', '机构数': 10}])
        with patch('stocks.market.institutions.timezone.localdate', return_value=date(2026, 10, 11)), \
                patch('akshare.stock_institute_hold', return_value=df) as hold:
            data = market.institutions.fetch_institute_hold_stocks()
        hold.assert_called_once_with(symbol='20262')
        self.assertEqual(data['quarter'], '20262')

    def test_shareholder_changes_never_request_future_dates(self):
        from datetime import date

        with patch('stocks.market.institutions.timezone.localdate', return_value=date(2026, 2, 15)), \
                patch('akshare.stock_gdfx_free_holding_change_em', return_value=pd.DataFrame()) as chg:
            market.institutions.fetch_institution_shareholder_changes()
        dates = [c.kwargs['date'] for c in chg.call_args_list]
        self.assertEqual(dates[0], '20250930')
        self.assertTrue(all(d <= '20250930' for d in dates))


class ThsVendoredTokenTests(SimpleTestCase):
    """DAT-17：hexin-v 由仓库内 ths.js 生成并按分钟复用；分页之间有间隔。"""

    def test_token_from_vendored_js_and_reused(self):
        from .market import sectors

        self.assertTrue(sectors._THS_JS_PATH.is_file())
        with patch('py_mini_racer.MiniRacer', wraps=__import__('py_mini_racer').MiniRacer) as racer:
            first = sectors._ths_hexin_v()
            second = sectors._ths_hexin_v()
        self.assertTrue(first)
        self.assertEqual(first, second)
        self.assertEqual(racer.call_count, 1)

    def test_no_private_akshare_import(self):
        import inspect

        from .market import sectors

        self.assertNotIn('akshare.stock_feature', inspect.getsource(sectors))

    def test_sleep_between_pages(self):
        from .market import sectors

        def html(page_info):
            cells = ['1', '半导体', '1000', '4.36%', '30', '14', '16.02', '50', '某股', '10.05%', '9.9']
            row = '<tr>' + ''.join(f'<td>{c}</td>' for c in cells) + '</tr>'
            return (f'<html><body><span class="page_info">{page_info}</span>'
                    f'<table>{row}</table></body></html>')

        pages = [html('1/3'), html('2/3'), html('3/3')]
        resp = lambda text: type('R', (), {'text': text, 'raise_for_status': lambda self: None})()  # noqa: E731
        with patch('stocks.market.sectors._ths_token_headers', return_value={}), \
                patch('stocks.market.sectors.requests.get', side_effect=lambda *a, **k: resp(pages.pop(0))), \
                patch('stocks.market.sectors.time.sleep') as sleep:
            df = sectors._ths_fund_flow('industry')
        self.assertEqual(len(df), 3)
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(sectors._THS_PAGE_INTERVAL)
