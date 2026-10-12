"""成分池：指数成分刷新（失败保留旧值、数量校验、补建 Stock 行）与管理命令。"""

from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase

from stocks.models import IndexConstituent, Stock
from stocks.pool import constituents as cons


def _rows(n, start=600000, name='股'):
    return [{'code': f'{start + i:06d}', 'name': f'{name}{i}', 'as_of': None} for i in range(n)]


def _fake_sources(by_index, tag='csindex'):
    def fn(index_code):
        value = by_index[index_code]
        if isinstance(value, Exception):
            raise value
        return value
    return [(tag, fn)]


class ConstituentRefreshTests(TestCase):
    def test_refresh_replaces_rows_and_creates_inactive_stocks(self):
        sources = _fake_sources({'000300': _rows(300), '000905': _rows(500, start=1)})
        with mock.patch.object(cons, 'SOURCES', sources):
            result = cons.refresh_constituents()
        self.assertEqual(result['000300']['status'], 'ok')
        self.assertEqual(IndexConstituent.objects.filter(index_code='000300').count(), 300)
        self.assertEqual(IndexConstituent.objects.filter(index_code='000905').count(), 500)
        self.assertEqual(len(cons.pool_members()), 800)
        # 成分股补建为非自选 Stock 行，不进自选列表
        self.assertEqual(Stock.objects.filter(is_active=False).count(), 800)
        self.assertFalse(Stock.objects.filter(is_active=True).exists())

    def test_existing_watchlist_stock_is_not_touched(self):
        Stock.objects.create(code='600000', name='浦发银行', is_active=True)
        with mock.patch.object(cons, 'SOURCES', _fake_sources({'000300': _rows(300), '000905': _rows(500, 1)})):
            cons.refresh_constituents()
        self.assertTrue(Stock.objects.get(code='600000').is_active)

    def test_failure_keeps_old_rows(self):
        with mock.patch.object(cons, 'SOURCES', _fake_sources({'000300': _rows(300), '000905': _rows(500, 1)})):
            cons.refresh_constituents()
        boom = _fake_sources({'000300': RuntimeError('proxy error'), '000905': _rows(500, 1)})
        with mock.patch.object(cons, 'SOURCES', boom):
            result = cons.refresh_constituents()
        self.assertEqual(result['000300']['status'], 'kept')
        self.assertEqual(result['000300']['count'], 300)
        self.assertEqual(IndexConstituent.objects.filter(index_code='000300').count(), 300)

    def test_truncated_list_is_rejected_and_old_kept(self):
        with mock.patch.object(cons, 'SOURCES', _fake_sources({'000300': _rows(300), '000905': _rows(500, 1)})):
            cons.refresh_constituents()
        # 成分文件截断成 120 行：数量校验不过，保留旧的 300 只
        with mock.patch.object(cons, 'SOURCES', _fake_sources({'000300': _rows(120), '000905': _rows(500, 1)})):
            result = cons.refresh_constituents(['000300'])
        self.assertEqual(result['000300']['status'], 'kept')
        self.assertEqual(IndexConstituent.objects.filter(index_code='000300').count(), 300)

    def test_fallback_source_used_when_primary_fails(self):
        def primary(_code):
            raise ConnectionError('csindex down')

        sources = [('csindex', primary), ('sina', lambda code: _rows(300) if code == '000300' else _rows(500, 1))]
        with mock.patch.object(cons, 'SOURCES', sources):
            result = cons.refresh_constituents()
        self.assertEqual(result['000300']['source'], 'sina')
        self.assertEqual(IndexConstituent.objects.filter(source='sina').count(), 800)

    def test_rebalance_removes_and_adds(self):
        with mock.patch.object(cons, 'SOURCES', _fake_sources({'000300': _rows(300), '000905': _rows(500, 1)})):
            cons.refresh_constituents()
        new = _rows(295) + _rows(5, start=688000)
        with mock.patch.object(cons, 'SOURCES', _fake_sources({'000300': new, '000905': _rows(500, 1)})):
            result = cons.refresh_constituents(['000300'])
        self.assertEqual((result['000300']['added'], result['000300']['removed']), (5, 5))
        self.assertTrue(IndexConstituent.objects.filter(code='688004').exists())
        self.assertFalse(IndexConstituent.objects.filter(code='600299').exists())

    def test_bad_code_rejected(self):
        with self.assertRaises(ValueError):
            cons.validate_rows('000300', [{'code': 'abc'}])


class RefreshCommandTests(TestCase):
    def test_command_ok(self):
        out = StringIO()
        with mock.patch.object(cons, 'SOURCES', _fake_sources({'000300': _rows(300), '000905': _rows(500, 1)})):
            call_command('refresh_index_constituents', stdout=out)
        self.assertIn('成分池合计 800 只', out.getvalue())

    def test_command_fails_nonzero_but_keeps_old(self):
        with mock.patch.object(cons, 'SOURCES', _fake_sources({'000300': _rows(300), '000905': _rows(500, 1)})):
            call_command('refresh_index_constituents', stdout=StringIO())
        with mock.patch.object(cons, 'SOURCES', _fake_sources({'000300': RuntimeError('x'), '000905': RuntimeError('y')})):
            with self.assertRaises(CommandError):
                call_command('refresh_index_constituents', stdout=StringIO(), stderr=StringIO())
        self.assertEqual(IndexConstituent.objects.count(), 800)


class CsindexParserTests(TestCase):
    """真实列名的 fixture（2026-10-12 沙箱抓取 index_stock_cons_csindex 的列结构）。"""

    def test_csindex_columns_parsed(self):
        import pandas as pd

        df = pd.DataFrame({
            '日期': ['2026-10-12', '2026-10-12'], '指数代码': ['000300', '000300'],
            '指数名称': ['沪深300', '沪深300'], '指数英文名称': ['x', 'x'],
            '成分券代码': ['1', '600519'], '成分券名称': ['平安银行', '贵州茅台'],
            '成分券英文名称': ['a', 'b'], '交易所': ['深圳证券交易所', '上海证券交易所'],
            '交易所英文名称': ['s', 's'],
        })
        with mock.patch('akshare.index_stock_cons_csindex', return_value=df):
            rows = cons._fetch_csindex('000300')
        self.assertEqual(rows[0]['code'], '000001')
        self.assertEqual(str(rows[0]['as_of']), '2026-10-12')
        self.assertEqual(rows[1]['name'], '贵州茅台')
