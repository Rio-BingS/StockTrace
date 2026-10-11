"""P3 data-detector 方向回归测试（DAT-17、ALG-13、DetectorSignal 约束、
东财 5d/10d 排行删除、detector 分页参数接入 common/params）。

全部离线：上游请求一律 mock。
"""

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase


def _ths_html(names, page_info):
    trs = ''
    for i, name in enumerate(names):
        cells = [str(i + 1), name, '1000', '1.00%', '30', '14', '16.02', '50', '某股', '10.05%', '9.9']
        trs += '<tr>' + ''.join(f'<td>{c}</td>' for c in cells) + '</tr>'
    return (f'<html><body><span class="page_info">{page_info}</span>'
            f'<table>{trs}</table></body></html>')


def _resp(text):
    return type('R', (), {'text': text, 'raise_for_status': lambda self: None})()


class ThsStableSortTests(SimpleTestCase):
    """DAT-17：同花顺翻页按稳定字段排序，跨页重复去重并留痕。"""

    def _run(self, pages):
        from .market import sectors

        urls = []

        def fake_get(url, **kwargs):
            urls.append(url)
            return _resp(pages.pop(0))

        with patch('stocks.market.sectors._ths_token_headers', return_value={}), \
                patch('stocks.market.sectors.requests.get', side_effect=fake_get), \
                patch('stocks.market.sectors.time.sleep'):
            df = sectors._ths_fund_flow('concept')
        return df, urls

    def test_pages_sorted_by_board_name(self):
        _df, urls = self._run([_ths_html(['A', 'B'], '1/2'), _ths_html(['C'], '2/2')])
        self.assertEqual(len(urls), 2)
        for page, url in enumerate(urls, start=1):
            self.assertIn(f'field/tradename/order/asc/page/{page}/', url)
            self.assertNotIn('tradezdf', url)

    def test_duplicate_across_pages_is_dropped_and_logged(self):
        pages = [_ths_html(['A', 'B'], '1/2'), _ths_html(['B', 'C'], '2/2')]
        with self.assertLogs('stocks.market.sectors', level='WARNING') as logs:
            df, _urls = self._run(pages)
        self.assertEqual(list(df['概念']), ['A', 'B', 'C'])
        self.assertTrue(any('1 行重复' in m for m in logs.output))

    def test_short_middle_page_is_logged(self):
        pages = [_ths_html(['A', 'B'], '1/3'), _ths_html(['C'], '2/3'), _ths_html(['D'], '3/3')]
        with self.assertLogs('stocks.market.sectors', level='WARNING') as logs:
            df, _urls = self._run(pages)
        self.assertEqual(len(df), 4)
        self.assertTrue(any('疑似漏行' in m and '[2]' in m for m in logs.output))

    def test_clean_pages_log_nothing(self):
        from .market import sectors

        pages = [_ths_html(['A', 'B'], '1/2'), _ths_html(['C'], '2/2')]
        with patch.object(sectors.logger, 'warning') as warn:
            df, _urls = self._run(pages)
        self.assertEqual(len(df), 3)
        warn.assert_not_called()


class EmSectorRankRemovedTests(SimpleTestCase):
    """东财板块 5日/10日排行抓取已删除：多日口径只走日度快照（DAT-15 收尾）。"""

    def test_rank_helpers_gone(self):
        import inspect

        from . import market
        from .market import sectors

        self.assertFalse(hasattr(sectors, '_parse_sector_rank_table'))
        self.assertFalse(hasattr(sectors, '_SECTOR_RANK_INDICATORS'))
        self.assertFalse(hasattr(market, '_parse_sector_rank_table'))
        self.assertNotIn('stock_sector_fund_flow_rank', inspect.getsource(sectors))

    def test_multi_day_period_rejected_without_upstream_call(self):
        from .market import sectors

        for func in (sectors.fetch_industry_fund_flow, sectors.fetch_concept_fund_flow):
            for period in ('5d', '10d'):
                with self.subTest(func=func.__name__, period=period), \
                        patch('stocks.market.sectors._ths_fund_flow_df') as ths:
                    with self.assertRaises(ValueError):
                        func(period=period)
                    ths.assert_not_called()

    def test_day_period_still_uses_ths(self):
        from .market import sectors

        with patch('stocks.market.sectors._ths_fund_flow_df', return_value=None) as ths:
            self.assertEqual(sectors.fetch_industry_fund_flow(), [])
        ths.assert_called_once_with('industry', 'ths_fund_flow_industry')


def _legacy_page_size(raw):
    """接入 common/params 前 detector/views._page_size 的原实现（对照用）。"""
    if raw in (None, ''):
        return 50
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 50
    return max(1, min(value, 100))


def _legacy_positive_int(raw, default, maximum=None):
    """接入前 detector/views._positive_int 的原实现（对照用）。"""
    if raw in (None, ''):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < 1:
        return default
    return min(value, maximum) if maximum else value


_PARAM_INPUTS = [None, '', '0', '-5', '1', '2', '50', '99', '100', '101', '1000', 'abc', '1.5', ' 3 ']


class DetectorParamsTests(SimpleTestCase):
    """detector/views 分页参数接入 common/params：逐值与旧实现一致。"""

    def test_page_size_matches_legacy_clamp(self):
        from .common.params import positive_int

        for raw in _PARAM_INPUTS:
            with self.subTest(raw=raw):
                params = {} if raw is None else {'page_size': raw}
                self.assertEqual(
                    positive_int(params, 'page_size', 50, 100, clamp=True), _legacy_page_size(raw),
                )

    def test_page_matches_legacy_fallback(self):
        from .common.params import positive_int

        for raw in _PARAM_INPUTS:
            with self.subTest(raw=raw):
                params = {} if raw is None else {'page': raw}
                self.assertEqual(positive_int(params, 'page', 1), _legacy_positive_int(raw, 1))

    def test_clamp_does_not_change_default_modes(self):
        from .common.params import positive_int
        from .errors import BadRequest

        self.assertEqual(positive_int({'n': '0'}, 'n', 7), 7)
        self.assertEqual(positive_int({'n': '0'}, 'n', 7, clamp=True), 1)
        with self.assertRaises(BadRequest):
            positive_int({'n': '0'}, 'n', 7, strict=True)
        # clamp 只管小于 1 的整数；strict 下非整数仍是 400
        self.assertEqual(positive_int({'n': '-3'}, 'n', 7, strict=True, clamp=True), 1)
        with self.assertRaises(BadRequest):
            positive_int({'n': 'x'}, 'n', 7, strict=True, clamp=True)

    def test_legacy_helpers_removed(self):
        from .detector import views as dv

        for name in ('_page_size', '_positive_int', '_parse_date'):
            self.assertFalse(hasattr(dv, name), name)


@patch('stocks.detector.views.expected_latest_date', lambda *a, **k: None)
class DetectorSignalsParamsApiTests(TestCase):
    """信号列表接口的分页/日期/all_dates 参数对外行为不变。"""

    def setUp(self):
        from datetime import date

        from .models import DetectorRun, DetectorSignal

        self.day = date(2026, 10, 9)
        DetectorRun.objects.create(trade_date=self.day, status='ok')
        for i in range(3):
            DetectorSignal.objects.create(
                target_kind='index', target_code=f'sh00000{i}', target_name=f'指数{i}',
                trade_date=self.day, direction='up', cycles=1, score=10 + i,
            )

    def _get(self, **params):
        from django.urls import reverse

        return self.client.get(reverse('detector-signals'), params)

    def test_page_size_zero_and_negative_clamp_to_one(self):
        for raw in ('0', '-5'):
            with self.subTest(raw=raw):
                body = self._get(page_size=raw).json()
                self.assertEqual(body['pagination']['page_size'], 1)
                self.assertEqual(len(body['items']), 1)

    def test_bad_page_size_and_page_fall_back(self):
        body = self._get(page_size='abc', page='0').json()
        self.assertEqual(body['pagination']['page_size'], 50)
        self.assertEqual(body['pagination']['page'], 1)
        body = self._get(page_size='1000').json()
        self.assertEqual(body['pagination']['page_size'], 100)

    def test_date_and_all_dates_parsing(self):
        self.assertEqual(self._get(date='2026/10/09').status_code, 400)
        self.assertEqual(self._get(date='2026-10-09').json()['summary']['total'], 3)
        # all_dates 只认小写 '1'/'true'：'TRUE' 视为未开启，按最新扫描日返回
        body = self._get(all_dates='TRUE').json()
        self.assertEqual(body['trade_date'], '2026-10-09')
