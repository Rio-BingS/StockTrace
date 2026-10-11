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


class LocalDateTests(SimpleTestCase):
    """DAT-18：「今天」一律取本地日期，时间字符串显式按 TIME_ZONE。"""

    # UTC 2026-10-11 17:30 = 北京时间 2026-10-12 01:30
    _UTC_NIGHT = __import__('datetime').datetime(
        2026, 10, 11, 17, 30, tzinfo=__import__('datetime').timezone.utc)

    def test_daily_start_date_uses_local_date(self):
        from . import services

        with patch('django.utils.timezone.now', return_value=self._UTC_NIGHT), \
                patch('stocks.services.DailyQuote.objects') as qs:
            qs.filter.return_value.order_by.return_value.values_list.return_value.first.return_value = None
            start = services._daily_start_date(object(), bootstrap_days=1)
        self.assertEqual(start, '20261011')  # 本地 10-12 往前 1 天；UTC 口径会是 10-10

    def test_now_str_and_fetched_at_are_local(self):
        import sys

        # stocks.market 包把 _cache 字典导出成了同名属性，这里取模块本身
        _cache = sys.modules['stocks.market._cache']
        with patch('django.utils.timezone.now', return_value=self._UTC_NIGHT):
            self.assertEqual(_cache._now_str(), '2026-10-12 01:30:00')
        self.assertEqual(
            _cache._local_iso(self._UTC_NIGHT.timestamp()), '2026-10-12T01:30:00+08:00',
        )

    def test_period_today_defaults_to_local_date(self):
        from .market.periods import resolve_period

        with patch('django.utils.timezone.now', return_value=self._UTC_NIGHT):
            window = resolve_period(lambda k, d='': {'period': '1d'}.get(k, d), ['1d'])
        self.assertEqual(window.end.isoformat(), '2026-10-12')


class MetaAccuracyTests(SimpleTestCase):
    """DAT-19：data_as_of 跟数据走；ETF 历史 items 与来源同一缓存值；份额曲线 meta 不再恒为 unavailable。"""

    def test_data_as_of_prefers_source_data_date(self):
        from .market._cache import _cache_meta, _cache_set

        _cache_set('k_meta', [1])
        meta = _cache_meta('k_meta', 60, 'src', True, source_data_date='2026-09-04')
        self.assertEqual(meta['data_as_of'], '2026-09-04')
        meta = _cache_meta('k_meta', 60, 'src', True)
        self.assertIsNone(meta['data_as_of'])  # 测试进程不查日历：未知如实 null

    def test_session_date_at_follows_write_time(self):
        from datetime import date, datetime

        from django.utils import timezone

        from .market._cache import _session_date_at

        def trading(d):
            return d.weekday() < 5

        with patch('stocks.services.is_trading_day', side_effect=trading):
            # 周一 08:00 写入（未开盘）→ 上周五；周一 10:00 → 周一
            mon_8 = timezone.make_aware(datetime(2026, 9, 7, 8, 0)).timestamp()
            mon_10 = timezone.make_aware(datetime(2026, 9, 7, 10, 0)).timestamp()
            self.assertEqual(_session_date_at(mon_8), date(2026, 9, 4).isoformat())
            self.assertEqual(_session_date_at(mon_10), date(2026, 9, 7).isoformat())
        with patch('stocks.services.is_trading_day', return_value=None):
            self.assertIsNone(_session_date_at(mon_10))

    @patch('stocks.market.etf.fetch_holder_structure', new=_no_holders)
    @patch('stocks.market.etf._normalized_etf_items', return_value=_QUOTE)
    def test_history_items_and_source_share_one_key(self, _items):
        from .market import _cache as _cache_dict
        from .market.etf import _history_cache_key

        sina_df = pd.DataFrame([
            {'date': '2026-08-03', 'open': 4.0, 'high': 4.1, 'low': 3.9, 'close': 4.0, 'volume': 1000},
        ])
        with patch('akshare.fund_etf_hist_em', side_effect=RuntimeError('502')), patch(
            'akshare.fund_etf_hist_sina', return_value=sina_df,
        ), patch('stocks.market.snapshots.share_history_series', return_value=[
            {'date': '2026-08-03', 'share': 1.0e10},
        ]):
            data = market.get_etf_detail(
                '510300', 'custom', start_date='2026-08-01', end_date='2026-08-31')
        from datetime import date

        key = _history_cache_key('510300', 'custom', date(2026, 8, 1), date(2026, 8, 31))
        self.assertNotIn(f'{key}_src', _cache_dict)
        self.assertEqual(_cache_dict[key]['data']['source'], 'sina_etf_hist')
        self.assertEqual(data['history']['meta']['source'], 'akshare.sina_etf_hist')
        share_meta = data['share_history']['meta']
        self.assertEqual(share_meta['cache_status'], 'fresh')
        self.assertEqual(share_meta['data_as_of'], '2026-08-03')


class IndexTrendEndOnlyTests(SimpleTestCase):
    """DAT-20：指数走势只传 end 时不再被忽略。"""

    def test_end_only_cuts_tail_before_end(self):
        from .market.indices import fetch_index_trend

        df = pd.DataFrame({
            'date': pd.date_range('2026-09-01', periods=10, freq='D'),
            'close': [float(i) for i in range(1, 11)],
            'volume': [1.0] * 10,
        })
        with patch('akshare.stock_zh_index_daily', return_value=df):
            data = fetch_index_trend(days=3, end='2026-09-05')
        items = data['series'][0]['items']
        self.assertEqual([i['date'] for i in items], ['2026-09-03', '2026-09-04', '2026-09-05'])
        self.assertEqual(data['end'], '2026-09-05')


class HolderShareUnitTests(SimpleTestCase):
    """DAT-20：持有人结构总份额统一为「份」并带单位字段。"""

    def test_total_shares_in_units_of_share(self):
        html = (
            'var apidata={ content:"<table><thead><tr><th>公告日期</th><th>机构持有比例</th>'
            '<th>个人持有比例</th><th>内部持有比例</th><th>总份额（亿份）</th></tr></thead><tbody>'
            '<tr><td>2026-06-30</td><td>61.65%</td><td>32.95%</td><td>5.40%</td><td>2.5</td></tr>'
            '</tbody></table>"};'
        )
        resp = type('R', (), {'text': html, 'raise_for_status': lambda self: None})()
        with patch('stocks.market.holders.requests.get', return_value=resp):
            d = market.holders.fetch_holder_structure('510300', force=True)
        self.assertEqual(d['items'][0]['total_shares'], 2.5e8)
        self.assertEqual(d['items'][0]['total_shares_unit'], '份')


class ValuationEntryTtlTests(SimpleTestCase):
    """DAT-20：估值缓存按条目计时，补拉新股票不续期旧条目。"""

    def test_old_entries_expire_independently(self):
        from .ai import valuation

        calls = []

        def fake_fetch(codes):
            calls.append(list(codes))
            return {c: {'pe_ttm': float(len(calls))} for c in codes}

        clock = [1_000_000.0]
        with patch('stocks.ai.valuation._fetch_raw', side_effect=fake_fetch), \
                patch('stocks.ai.valuation.time.time', side_effect=lambda: clock[0]), \
                patch('stocks.market._cache.time.time', side_effect=lambda: clock[0]):
            valuation.fetch_valuation_map(['600000'])
            clock[0] += 200
            valuation.fetch_valuation_map(['600000', '000001'])  # 只补拉 000001
            clock[0] += 200  # 600000 已 400s，000001 才 200s
            result = valuation.fetch_valuation_map(['600000', '000001'])
        self.assertEqual(calls, [['600000'], ['000001'], ['600000']])
        self.assertEqual(result['600000']['pe_ttm'], 3.0)
        self.assertEqual(result['000001']['pe_ttm'], 2.0)
