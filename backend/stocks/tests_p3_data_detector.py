"""P3 data-detector 方向回归测试（DAT-17、ALG-13、DetectorSignal 约束、
东财 5d/10d 排行删除、detector 分页参数接入 common/params）。

全部离线：上游请求一律 mock。
"""

from unittest.mock import patch

from django.test import SimpleTestCase


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
