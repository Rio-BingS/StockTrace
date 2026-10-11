"""ARC 第二轮 services 方向：公开 API、依赖方向与顺带修复的回归测试。"""

import ast
from pathlib import Path

from django.test import SimpleTestCase

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

    def test_old_code_helpers_match_new(self):
        from stocks import services
        from stocks.common.codes import exchange_prefix

        for code in ('600000', '000001', '830799', '510300', '1'):
            self.assertEqual(services._to_shsz_prefix(code), exchange_prefix(code))
        src = services.BaoStockSource()
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
