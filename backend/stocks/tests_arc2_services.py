"""ARC 第二轮 services 方向：公开 API、依赖方向与顺带修复的回归测试。"""

import ast
from datetime import date, datetime, timezone as dt_timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from django.test import SimpleTestCase, TestCase

STOCKS_DIR = Path(__file__).resolve().parent


def _imported_names(rel_path):
    """返回文件里所有 from-import 的 (模块, 名字) 列表（含函数内导入）"""
    tree = ast.parse((STOCKS_DIR / rel_path).read_text(encoding='utf-8'))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                out.append((('.' * node.level) + (node.module or ''), alias.name))
    return out


class PublicApiTests(SimpleTestCase):
    def test_paginate_clamps_and_reports(self):
        from stocks.common.pagination import paginate
        from stocks.market._query import _paginate

        self.assertIs(_paginate, paginate)
        items, info = paginate(list(range(5)), 9, 2)
        self.assertEqual(items, [4])
        self.assertEqual(info, {'page': 3, 'page_size': 2, 'total': 5, 'total_pages': 3})
        items, info = paginate([], 1, 50)
        self.assertEqual((items, info['total_pages'], info['page']), ([], 1, 1))

    def test_cache_public_names_alias_private(self):
        import importlib

        # stocks.market 包把 _cache（字典）导出到了包命名空间，须按模块路径取
        c = importlib.import_module('stocks.market._cache')

        self.assertIs(c._cache_get, c.cache_get)
        self.assertIs(c._cache_set, c.cache_set)
        self.assertIs(c._is_fresh, c.is_fresh)
        self.assertIs(c._stale_or, c.stale_or)
        c.cache_set('arc2_k', [1])
        self.assertEqual(c.cache_get('arc2_k', 60), [1])
        self.assertEqual(c.stale_or('arc2_k', None), [1])

    def test_exchange_prefix(self):
        from stocks.common.codes import exchange, exchange_prefix

        cases = {'600000': 'sh', '510300': 'sh', '900901': 'sh', '000001': 'sz',
                 '300750': 'sz', '830799': 'bj', '430047': 'bj', '920001': 'bj'}
        for code, ex in cases.items():
            self.assertEqual(exchange(code), ex, code)
            self.assertEqual(exchange_prefix(code), f'{ex}{code}')
            self.assertEqual(exchange_prefix(code, sep='.'), f'{ex}.{code}')
        self.assertEqual(exchange_prefix('1'), 'sz000001')

    def test_baostock_format_code_uses_exchange_prefix(self):
        from stocks.datasources import BaoStockSource

        src = BaoStockSource()
        self.assertEqual(src._format_code('600000'), 'sh.600000')
        self.assertEqual(src._format_code('1'), 'sz.000001')
        self.assertIsNone(src._format_code('830799'))

    def test_market_exports_public_parsers(self):
        from stocks import market

        self.assertIs(market.etf_row_to_item, market._etf_row_to_item)
        self.assertIs(market.parse_fund_flow_table, market._parse_fund_flow_table)
        self.assertIn('etf_row_to_item', market.__all__)
        self.assertIn('parse_fund_flow_table', market.__all__)


class DependencyDirectionTests(SimpleTestCase):
    def test_no_cross_package_private_imports(self):
        """ai/valuation、detector/views、校验命令不再跨包导入私有名（ARC-01 步骤 1）"""
        targets = {
            'ai/valuation.py': ('services', 'market._cache'),
            'detector/views.py': ('market._query',),
            'management/commands/validate_market_sources.py': ('stocks.market',),
        }
        for rel, modules in targets.items():
            for module, name in _imported_names(rel):
                if module.lstrip('.').endswith(modules):
                    self.assertFalse(name.startswith('_'), f'{rel}: {module}.{name}')

    def test_callers_use_trading_calendar_directly(self):
        """调度/提醒/探测器直接依赖 trading_calendar，不再绕道 services（ARC-02 收尾）"""
        for rel in ('scheduler.py', 'alerts.py', 'detector/scan.py', 'detector/signals.py'):
            pairs = _imported_names(rel)
            self.assertNotIn(('.services', 'is_trading_day'), pairs, rel)
            self.assertNotIn(('..services', 'is_trading_day'), pairs, rel)
            self.assertTrue(
                any(m.lstrip('.') == 'trading_calendar' and n == 'is_trading_day'
                    for m, n in pairs), rel)

    def test_common_does_not_import_business_modules(self):
        for path in (STOCKS_DIR / 'common').glob('*.py'):
            for module, _ in _imported_names(f'common/{path.name}'):
                self.assertFalse(
                    module.lstrip('.').split('.')[0] in ('services', 'market', 'detector', 'views'),
                    f'{path.name}: {module}')


class LocalDateTodayTests(SimpleTestCase):
    """候选日与两融区间按项目时区取「今天」，不受进程系统时区影响（VPS 为 UTC 时）"""

    # 北京时间 2026-09-08（周二）01:00 = UTC 2026-09-07 17:00
    _NOW = datetime(2026, 9, 7, 17, 0, tzinfo=dt_timezone.utc)

    def test_recent_day_candidates_uses_local_date(self):
        from stocks import trading_calendar as cal

        with patch('django.utils.timezone.now', return_value=self._NOW), \
                patch('stocks.trading_calendar.is_trading_day', return_value=True):
            self.assertEqual(cal.recent_day_candidates(1), [date(2026, 9, 8)])

    def test_zt_sentiment_first_candidate_is_local_today(self):
        from stocks.market import sentiment

        seen = []

        def fake_pool(date):
            seen.append(date)
            raise RuntimeError('down')

        with patch('django.utils.timezone.now', return_value=self._NOW), \
                patch('stocks.trading_calendar.is_trading_day', return_value=True), \
                patch('akshare.stock_zt_pool_em', side_effect=fake_pool):
            sentiment.fetch_zt_sentiment(force=True)
        self.assertEqual(seen[0], '20260908')

    def test_margin_range_ends_on_local_today(self):
        from stocks.market import sentiment

        with patch('django.utils.timezone.now', return_value=self._NOW), \
                patch('akshare.stock_margin_sse', return_value=pd.DataFrame()) as sse:
            sentiment._sh_margin_rows()
        self.assertEqual(sse.call_args.kwargs['end_date'], '20260908')
        self.assertEqual(sse.call_args.kwargs['start_date'], '20260818')


class TrySourcesLoggingTests(SimpleTestCase):
    """全部源只返回空时不打 ERROR；有异常时 ERROR 带上最后一个真实错误"""

    def _src(self, name, behavior):
        from stocks.datasources import DataSource

        class _S(DataSource):
            def fetch_daily(self, *a):
                if behavior == 'raise':
                    raise RuntimeError(f'{name} boom')
                return pd.DataFrame()

        s = _S()
        s.name = name
        return s

    def test_all_empty_logs_warning_not_error(self):
        from stocks.datasources import _try_sources

        with patch('stocks.datasources.chain.time.sleep'), \
                self.assertLogs('stocks.datasources.chain', level='WARNING') as cm:
            res = _try_sources('fetch_daily', '600000', '20260901', '20260901',
                               sources=[self._src('e1', 'empty'), self._src('e2', 'empty')])
        self.assertEqual(res, (None, None))
        self.assertFalse([r for r in cm.records if r.levelname == 'ERROR'])
        self.assertTrue(any('均返回空数据' in m for m in cm.output))
        self.assertFalse(any('None' in m for m in cm.output))

    def test_exception_logs_error_with_last_error(self):
        from stocks.datasources import _try_sources

        with patch('stocks.datasources.chain.time.sleep'), \
                self.assertLogs('stocks.datasources.chain', level='ERROR') as cm:
            _try_sources('fetch_daily', '600000', '20260901', '20260901',
                         sources=[self._src('x1', 'raise'), self._src('x2', 'empty')])
        self.assertTrue(any('最后错误 [x1]: x1 boom' in m for m in cm.output))


class FetchStockAllDataNameTests(TestCase):
    def test_name_lookup_failure_logs_warning_and_continues(self):
        from stocks import jobs
        from stocks.models import Stock

        stock = Stock.objects.create(code='600000', name='600000')
        with patch('stocks.jobs.fetch_stock_info', side_effect=RuntimeError('name down')), \
                patch('stocks.jobs.fetch_daily_data', return_value=3) as daily, \
                patch('stocks.trading_calendar.is_trading_day', return_value=False), \
                self.assertLogs('stocks.jobs', level='WARNING') as cm:
            self.assertEqual(jobs.fetch_stock_all_data(stock), 3)
        daily.assert_called_once()
        self.assertTrue(any('600000' in m and 'name down' in m for m in cm.output))
        stock.refresh_from_db()
        self.assertEqual(stock.name, '600000')


class DailySummaryLocalDateTests(TestCase):
    """收盘汇总的日期区间按北京时间取今天（DAT-18 口径，防回退到 timezone.now().date()）"""

    def test_summary_range_uses_local_date(self):
        from stocks import jobs
        from stocks.models import Stock

        Stock.objects.create(code='600000', name='浦发银行')
        utc_now = datetime(2026, 9, 7, 17, 0, tzinfo=dt_timezone.utc)  # 北京 09-08 01:00
        with patch('django.utils.timezone.now', return_value=utc_now), \
                patch('stocks.trading_calendar.is_trading_day', return_value=True), \
                patch('stocks.jobs.fetch_daily_data') as daily, \
                patch('stocks.jobs.fetch_minute_data'), \
                patch('stocks.jobs.time.sleep'):
            jobs.fetch_daily_summary()
        self.assertEqual(daily.call_args.kwargs,
                         {'start_date': '20260905', 'end_date': '20260908'})
