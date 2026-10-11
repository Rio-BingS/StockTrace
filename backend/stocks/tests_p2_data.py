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
