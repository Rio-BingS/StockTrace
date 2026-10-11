"""P3 cleanup：全局日志清理（步骤 5d）与删除兼容别名的回归测试。"""

import logging
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pandas as pd
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from .models import DailyQuote, Stock


def _levels(cm):
    return [r.levelno for r in cm.records]


class DataLayerLoggingTests(TestCase):
    """5d 数据层：空结果降级 warning、单行解析收窄、批量边界记堆栈、吞异常补日志"""

    def setUp(self):
        self.stock = Stock.objects.create(code='600000', name='浦发银行')

    def test_daily_empty_result_is_warning_not_error(self):
        from stocks.ingest import fetch_daily_data

        with patch('stocks.ingest._try_sources', return_value=(None, None)):
            with self.assertLogs('stocks.ingest', level='DEBUG') as cm:
                self.assertEqual(fetch_daily_data(self.stock), 0)
        self.assertIn(logging.WARNING, _levels(cm))
        self.assertNotIn(logging.ERROR, _levels(cm))

    def test_minute_empty_result_is_warning_not_error(self):
        from stocks.ingest import fetch_minute_data

        with patch('stocks.ingest._try_sources', return_value=(None, None)):
            with self.assertLogs('stocks.ingest', level='DEBUG') as cm:
                self.assertIsNone(fetch_minute_data(self.stock, date(2026, 9, 1)))
        self.assertNotIn(logging.ERROR, _levels(cm))

    def test_zero_prev_close_row_still_skipped_after_narrowing(self):
        """昨收为 0 时 Decimal 除零：收窄后仍是逐行跳过（不抛出），与收窄前一致。

        跳过的行不推进昨收链，所以后一行同样以 0 为昨收被跳过（旧行为，不在本次修正）。
        """
        from stocks.datasources import SinaSource
        from stocks.ingest import fetch_daily_data

        d0 = timezone.localdate() - timedelta(days=3)
        DailyQuote.objects.create(
            stock=self.stock, trade_date=d0 - timedelta(days=1), open_price=Decimal('0'),
            close_price=Decimal('0'), high_price=Decimal('0'), low_price=Decimal('0'),
            open_close_diff=0, open_close_pct=0, high_low_diff=0, high_low_pct=0,
        )
        df = pd.DataFrame([
            {'日期': d0.isoformat(), '开盘': 10, '收盘': 10, '最高': 10, '最低': 10,
             '成交量': 1, '成交额': 1.0},
            {'日期': (d0 + timedelta(days=1)).isoformat(), '开盘': 10, '收盘': 11, '最高': 11,
             '最低': 10, '成交量': 1, '成交额': 1.0},
        ])
        with patch('stocks.ingest._try_sources', return_value=(SinaSource(), df)):
            with patch('stocks.ingest._adjust_basis_changed', return_value=[]):
                with self.assertLogs('stocks.ingest', level='WARNING') as cm:
                    n = fetch_daily_data(self.stock)
        self.assertEqual(n, 0)
        self.assertEqual(sum('解析 600000 日K行数据失败' in m for m in cm.output), 2)

    def test_batch_failure_logs_traceback(self):
        from stocks.jobs import fetch_all_active_stocks

        with patch('stocks.jobs.fetch_stock_all_data', side_effect=RuntimeError('boom')), \
                patch('stocks.jobs.time.sleep'):
            with self.assertLogs('stocks.jobs', level='ERROR') as cm:
                res = fetch_all_active_stocks()
        self.assertEqual(res[0]['status'], 'error')
        self.assertTrue(cm.output[0].startswith('ERROR:stocks.jobs:拉取 600000 数据失败: boom'))
        self.assertIsNotNone(cm.records[0].exc_info)


class DataSourceLoggingTests(SimpleTestCase):
    def test_sina_name_failure_is_logged(self):
        from stocks.datasources import SinaSource

        with patch('stocks.datasources.sina.ak.stock_zh_a_spot', side_effect=ValueError('bad')):
            with self.assertLogs('stocks.datasources.sina', level='WARNING') as cm:
                self.assertIsNone(SinaSource().fetch_stock_name('600000'))
        self.assertIn('600000', cm.output[0])

    def test_stock_list_failure_falls_back_with_traceback(self):
        from stocks import search

        with patch.dict(search._stock_list_cache, {'df': None, 'expires': 0}), \
                patch('stocks.search.ak.stock_info_a_code_name', side_effect=OSError('net')):
            with self.assertLogs('stocks.search', level='WARNING') as cm:
                df = search._get_stock_list()
        self.assertTrue(df.empty)
        self.assertIsNotNone(cm.records[0].exc_info)

    def test_calendar_unexpected_error_logged(self):
        from stocks import trading_calendar

        with patch('stocks.trading_calendar.is_trading_day', side_effect=RuntimeError('x')):
            with self.assertLogs('stocks.trading_calendar', level='WARNING'):
                self.assertIsNone(trading_calendar.expected_latest_date())


class SchedulerLoggingTests(SimpleTestCase):
    """5d 调度：文件锁收窄到 OSError，原先静默吞掉的解锁/关闭/停止失败补 debug 日志"""

    def test_lock_open_failure_returns_false(self):
        from stocks import scheduler as sch

        with patch.object(sch, '_lock_fh', None), \
                patch('builtins.open', side_effect=PermissionError('ro')), \
                self.assertLogs('stocks.scheduler', level='WARNING') as cm:
            self.assertFalse(sch._acquire_lock())
        self.assertIn('调度文件锁获取失败', cm.output[0])

    def test_release_close_failure_logged(self):
        from unittest.mock import MagicMock

        from stocks import scheduler as sch

        fh = MagicMock()
        fh.close.side_effect = OSError('io')
        with patch.object(sch, '_lock_fh', fh), patch('fcntl.flock'), \
                self.assertLogs('stocks.scheduler', level='DEBUG') as cm:
            sch._release_lock()
            self.assertIsNone(sch._lock_fh)
        self.assertTrue(any('关闭调度锁文件失败' in m for m in cm.output))

    def test_stop_shutdown_error_still_releases(self):
        from unittest.mock import MagicMock

        from stocks import scheduler as sch

        fake = MagicMock()
        fake.shutdown.side_effect = RuntimeError('not running')
        with patch.object(sch, 'scheduler', fake), patch.object(sch, 'write_heartbeat'), \
                patch.object(sch, '_release_lock') as rel, \
                self.assertLogs('stocks.scheduler', level='DEBUG') as cm:
            sch.stop()
            self.assertIsNone(sch.scheduler)
        rel.assert_called_once()
        self.assertTrue(any('调度器停止异常' in m for m in cm.output))
        sch._retry_stop.clear()


class MarketLoggingTests(TestCase):
    """5d market 包：查库降级补日志、上游边界、预热失败带堆栈、requests 收窄"""

    def test_snapshot_read_failure_logged_and_degrades(self):
        from django.db import DatabaseError

        from stocks.market import snapshots

        with patch('stocks.market.snapshots.MarketDailySnapshot.objects') as qs, \
                self.assertLogs('stocks.market.snapshots', level='WARNING') as cm:
            qs.filter.side_effect = DatabaseError('locked')
            self.assertEqual(snapshots.market_ff_snapshot_rows(), [])
        self.assertIsNotNone(cm.records[0].exc_info)

    def test_sz_margin_expected_failure_is_debug(self):
        from stocks.market import sentiment

        with patch('stocks.market.sentiment.ak.stock_margin_szse', side_effect=KeyError('x')), \
                self.assertLogs('stocks.market.sentiment', level='DEBUG') as cm:
            self.assertIsNone(sentiment._sz_margin_row(date(2026, 9, 1)))
        self.assertEqual(_levels(cm), [logging.DEBUG])

    def test_warmup_failure_logs_warning_with_traceback(self):
        from stocks.market import overview

        boom = patch.multiple(
            'stocks.market.overview',
            fetch_hsgt_flow=lambda **k: (_ for _ in ()).throw(RuntimeError('a')),
            fetch_market_fund_flow_hist=lambda **k: None,
            _fetch_etf_spot_df=lambda **k: None,
            get_national_team_etfs=lambda **k: None,
            get_etf_share_radar=lambda **k: None,
            get_market_overview=lambda: None,
        )
        with boom, patch('stocks.market.overview.time.sleep'), \
                patch('stocks.market.snapshots.save_daily_snapshots', return_value={}), \
                patch('stocks.market.snapshots.verify_daily_snapshots', return_value=None), \
                self.assertLogs('stocks.market.overview', level='WARNING') as cm:
            overview.warm_post_close_lagging()
        rec = [r for r in cm.records if '预热北向资金失败' in r.getMessage()]
        self.assertEqual(rec[0].levelno, logging.WARNING)
        self.assertIsNotNone(rec[0].exc_info)

    def test_holders_request_exception_still_degrades(self):
        """holders 收窄到 RequestException 后，超时等网络错误仍降级为不可用"""
        import requests

        from stocks.market import holders

        with patch('stocks.market.holders.requests.get', side_effect=requests.Timeout('t')):
            d = holders.fetch_holder_structure('510300', force=True)
        self.assertFalse(d['available'])


class DetectorLoggingTests(SimpleTestCase):
    """5d 探测器：原先静默降级的分支补日志，降级结果不变"""

    def test_dimension_exception_logged_and_degraded(self):
        from stocks.detector import signals

        def bad(ctx, params):
            raise ZeroDivisionError('div')

        cfg = {'key': 'k1', 'name': '维度', 'weight': 1, 'params': {}, 'targets': ['stock'],
               'evaluate': bad}
        ctx = {'kind': 'stock', 'code': '600000', 'as_of': '2026-09-01'}
        with patch('stocks.detector.signals._count_cycles', return_value=0), \
                self.assertLogs('stocks.detector.signals', level='WARNING') as cm:
            _sig, dims = signals.evaluate_target(ctx, [cfg])
        self.assertFalse(dims['k1']['available'])
        self.assertIn('k1', cm.output[0])

    def test_previous_trading_days_unexpected_error_logged(self):
        from stocks.detector import signals

        with patch('stocks.trading_calendar.is_trading_day', side_effect=RuntimeError('x')), \
                self.assertLogs('stocks.detector.signals', level='WARNING'):
            self.assertIsNone(signals._previous_trading_days(date(2026, 9, 10), 3))


class AlertPushTests(SimpleTestCase):
    def test_push_request_error_degrades(self):
        import requests

        from stocks import alerts

        with self.settings(STOCKTRACE_PUSH_URL='http://push.invalid/x'), \
                patch('stocks.alerts.requests.post', side_effect=requests.ConnectionError('down')), \
                self.assertLogs('stocks.alerts', level='WARNING'):
            self.assertFalse(alerts.push_message('t', 'd'))


class BackfillInvalidDateTests(TestCase):
    """5d 管理命令：大盘资金流回补的日期解析收窄到 TypeError/ValueError 后，坏日期仍只计入跳过"""

    def test_bad_dates_are_skipped_not_raised(self):
        from io import StringIO

        from django.core.management import call_command

        from stocks.market import flows

        good = timezone.localdate() - timedelta(days=5)
        items = [
            {'date': 'x', 'main_net': 1.0},
            {'date': [1], 'main_net': 1.0},
            {'date': good.isoformat(), 'main_net': 1.0e10, 'small_net': 1.0, 'mid_net': 1.0,
             'large_net': 1.0, 'super_net': 1.0},
        ]
        hist = {'available': True, 'source': flows.MARKET_FUND_SOURCE_EM, 'items': items, 'message': ''}
        out = StringIO()
        with patch('stocks.market.flows.fetch_market_fund_flow_hist', return_value=hist), \
                patch('stocks.trading_calendar.expected_latest_date', return_value=good):
            call_command('backfill_market_snapshots', stdout=out, stderr=StringIO())
        from stocks.models import MarketDailySnapshot

        self.assertTrue(MarketDailySnapshot.objects.filter(
            kind=MarketDailySnapshot.KIND_MARKET_FF, trade_date=good).exists())
        self.assertIn('无效/未收盘跳过 2 天', out.getvalue())


class RuffConfigTests(SimpleTestCase):
    def test_logging_rules_enabled_globally(self):
        import tomllib
        from pathlib import Path

        cfg = tomllib.loads((Path(__file__).resolve().parent.parent / 'ruff.toml').read_text(encoding='utf-8'))
        rules = cfg['lint']['select'] + cfg['lint'].get('extend-select', [])
        for rule in ('G004', 'TRY400', 'BLE001'):
            self.assertIn(rule, rules)
        # 生产代码不得整文件豁免这三条，只有测试文件豁免 BLE001
        for pattern, ignored in cfg['lint'].get('per-file-ignores', {}).items():
            if {'G004', 'TRY400', 'BLE001'} & set(ignored):
                self.assertEqual((pattern, ignored), ('stocks/tests*.py', ['BLE001']))


class CompatAliasRemovedTests(SimpleTestCase):
    """删除 ARC 二轮留下的兼容别名（v1.6.0 已带一版）：旧路径不可再导入，新路径照常可用"""

    def test_ai_alias_modules_removed(self):
        import importlib

        for old in ('stocks.ai_serializers', 'stocks.ai.screener', 'stocks.ai.strategies'):
            with self.assertRaises(ModuleNotFoundError, msg=old):
                importlib.import_module(old)
        from stocks.ai import serializers  # noqa: F401
        from stocks.screener import engine, strategies  # noqa: F401

    def test_services_facade_removed(self):
        import importlib
        from pathlib import Path

        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module('stocks.services')
        # 生产代码不得再引用外观（测试文件里只剩依赖方向断言）
        root = Path(__file__).resolve().parent
        for path in root.rglob('*.py'):
            if path.name.startswith('tests') or 'migrations' in path.parts:
                continue
            text = path.read_text(encoding='utf-8')
            for needle in ('from .services', 'from ..services', 'stocks.services'):
                self.assertNotIn(needle, text, str(path))
