"""OPS-08 覆盖率补齐：管理命令（validate_market_sources / backfill_etf_share_sse /
fetch_stock_data / run_scheduler / run_detector_scan）。

上游 akshare 与重活入口 mock 掉，验证命令自身的分支、输出与「只读/幂等」承诺。
"""

import signal
from datetime import date
from io import StringIO
from unittest.mock import patch

import pandas as pd
from django.core.management import call_command
from django.test import TestCase

from .models import MarketDailySnapshot, Stock


def _run(name, *args, **kwargs):
    out, err = StringIO(), StringIO()
    call_command(name, *args, stdout=out, stderr=err, **kwargs)
    return out.getvalue(), err.getvalue()


def _etf_spot():
    return pd.DataFrame({
        '代码': ['510300', '159915', '511010'],
        '名称': ['沪深300ETF', '创业板ETF', '国债ETF'],
        '最新份额': [1e9, 2e9, None], '成交额': [1.0, 2.0, 3.0],
        '总市值': [1.0, 2.0, 3.0], '流通市值': [1.0, 2.0, 3.0],
        '数据日期': ['2026-10-09'] * 3, '更新时间': ['15:00'] * 3,
    })


def _flow_table(name_col):
    return pd.DataFrame({name_col: ['半导体', '银行'], '净额': [1.5, -0.5],
                         '流入资金': [3.0, 1.0], '流出资金': [1.5, 1.5]})


class ValidateMarketSourcesTests(TestCase):
    def _patches(self, **over):
        hist = pd.DataFrame({'日期': ['2026-01-05', '2026-10-09'], '收盘': [1.0, 1.1]})
        base = {
            'akshare.fund_etf_spot_em': dict(return_value=_etf_spot()),
            'akshare.stock_fund_flow_industry': dict(return_value=_flow_table('行业')),
            'akshare.stock_fund_flow_concept': dict(return_value=_flow_table('概念')),
            'akshare.fund_etf_hist_em': dict(return_value=hist),
            'akshare.stock_market_fund_flow': dict(return_value=pd.DataFrame({'日期': ['2026-10-09']})),
            'stocks.market.flows._normalize_market_fund_hist': dict(return_value={'items': [
                {'date': '2026-10-09', 'main_net': 100.0, 'super_net': 1.0, 'large_net': None,
                 'mid_net': 1.0, 'small_net': 1.0}]}),
            'stocks.market.snapshots.fetch_market_ff_merged_rows': dict(
                return_value=({'2026-10-09': {'main_net': 100.0}}, [])),
        }
        base.update(over)
        return [patch(target, **kw) for target, kw in base.items()]

    def _call(self, *args, **over):
        ps = self._patches(**over)
        for p in ps:
            p.start()
        try:
            return _run('validate_market_sources', *args)
        finally:
            for p in reversed(ps):
                p.stop()

    def test_happy_path_reports_all_sections_read_only(self):
        out, _ = self._call()
        for text in ('ETF 全市场快照', '返回 3 行', '最新份额: 非空 2/3', '基金折价率: 字段缺失',
                     '沪市宽基 510300', '跨境 513100: 快照中不存在', '行业: 上游 2 行，规范化 2 行',
                     '净流入 1，净流出 1', '覆盖 2026-01-05 → 2026-10-09', '比值=1.0', '校验流程结束'):
            self.assertIn(text, out)
        self.assertFalse(MarketDailySnapshot.objects.exists())

    def test_each_upstream_failure_is_isolated(self):
        boom = dict(side_effect=RuntimeError('down'))
        out, _ = self._call(**{
            'akshare.fund_etf_spot_em': boom,
            'akshare.stock_fund_flow_industry': boom,
            'akshare.stock_fund_flow_concept': boom,
            'akshare.fund_etf_hist_em': boom,
            'akshare.stock_market_fund_flow': boom,
        })
        for label in ('ETF 全表失败', '行业资金失败', '概念资金失败', '日线失败', 'stock_market_fund_flow失败'):
            self.assertIn(label, out)
        self.assertIn('校验流程结束', out)

    def test_history_codes_and_degraded_market_flow(self):
        out, _ = self._call(
            '--history-code', '1', '--history-code', '510500',
            **{'akshare.fund_etf_hist_em': dict(return_value=pd.DataFrame()),
               'stocks.market.snapshots.fetch_market_ff_merged_rows': dict(
                   return_value=({}, ['sz: down']))})
        self.assertIn('指定样本 ETF 000001: 日线为空', out)
        self.assertIn('指定样本 ETF 510500: 日线为空', out)
        self.assertIn('自采沪深 fflow 部分失败', out)
        self.assertIn('无同日数据，无法对照', out)

    def test_market_flow_unparseable(self):
        out, _ = self._call(**{'stocks.market.flows._normalize_market_fund_hist': dict(return_value=None)})
        self.assertIn('规范化结果为空', out)


class BackfillEtfShareSseTests(TestCase):
    """仅沪市回填：已有快照日跳过、无数据日不写行、日历不可用中止。"""

    def _cal(self, d=None):
        return d.weekday() < 5

    def test_backfills_missing_days_and_skips_existing(self):
        MarketDailySnapshot.objects.create(
            kind=MarketDailySnapshot.KIND_ETF_SHARE, trade_date=date(2026, 10, 8), payload=[{'x': 1}])

        def rows(day):
            return [] if day == date(2026, 10, 7) else [{'code': '510300', 'share': 1.0}]

        with patch('stocks.trading_calendar.expected_latest_date', return_value=date(2026, 10, 9)), \
                patch('stocks.trading_calendar.is_trading_day', side_effect=self._cal), \
                patch('stocks.market.snapshots.fetch_sse_share_rows', side_effect=rows) as fetch, \
                patch('stocks.management.commands.backfill_etf_share_sse.time.sleep'):
            out, _ = _run('backfill_etf_share_sse', '--days', '3', '--sleep', '0')
        self.assertEqual([c.args[0] for c in fetch.call_args_list], [date(2026, 10, 9), date(2026, 10, 7)])
        self.assertIn('写入 1 天，已有快照跳过 1 天，无数据 1 天', out)
        snaps = MarketDailySnapshot.objects.filter(kind=MarketDailySnapshot.KIND_ETF_SHARE)
        self.assertEqual(sorted(snaps.values_list('trade_date', flat=True)),
                         [date(2026, 10, 8), date(2026, 10, 9)])
        self.assertEqual(snaps.get(trade_date=date(2026, 10, 8)).payload, [{'x': 1}])  # 不覆盖

    def test_calendar_unavailable_or_error_aborts(self):
        with patch('stocks.trading_calendar.expected_latest_date', return_value=None), \
                patch('stocks.market.snapshots.fetch_sse_share_rows') as fetch:
            _, err = _run('backfill_etf_share_sse')
        self.assertIn('交易日历不可用', err)
        with patch('stocks.trading_calendar.expected_latest_date', return_value=date(2026, 10, 9)), \
                patch('stocks.trading_calendar.is_trading_day', side_effect=RuntimeError('x')):
            _, err = _run('backfill_etf_share_sse')
        self.assertIn('交易日历查询异常', err)
        with patch('stocks.trading_calendar.expected_latest_date', return_value=date(2026, 10, 9)), \
                patch('stocks.trading_calendar.is_trading_day', return_value=None):
            _, err = _run('backfill_etf_share_sse', '--days', '2')
        self.assertIn('未取得任何交易日', err)
        fetch.assert_not_called()


class FetchStockDataCommandTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(code='600000', name='浦发银行')

    def test_argument_validation(self):
        out, _ = _run('fetch_stock_data')
        self.assertIn('请指定股票代码或使用 --all', out)
        out, _ = _run('fetch_stock_data', '600000', '--source', 'sina')
        self.assertIn('--source 仅能与 --full-daily 一起使用', out)
        out, _ = _run('fetch_stock_data', '--full-daily')
        self.assertIn('请指定股票代码或使用 --all', out)
        out, _ = _run('fetch_stock_data', '999999', '--full-daily')
        self.assertIn('不在关注列表中', out)
        out, _ = _run('fetch_stock_data', '999999')
        self.assertIn('不在关注列表中', out)

    def test_single_and_all(self):
        with patch('stocks.management.commands.fetch_stock_data.fetch_stock_all_data', return_value=9) as one:
            out, _ = _run('fetch_stock_data', '600000', '--days', '3')
        self.assertEqual(one.call_args.kwargs['days_back'], 3)
        self.assertIn('完成，9 条日K线数据', out)
        results = [{'code': '600000', 'status': 'success', 'count': 2},
                   {'code': '000001', 'status': 'error', 'message': 'boom'}]
        with patch('stocks.management.commands.fetch_stock_data.fetch_all_active_stocks', return_value=results):
            out, _ = _run('fetch_stock_data', '--all')
        self.assertIn('600000: 成功，2 条日K线', out)
        self.assertIn('000001: 失败 - boom', out)

    def test_full_daily_all_reports_per_stock(self):
        Stock.objects.create(code='000001', name='平安银行')
        Stock.objects.create(code='000002', name='万科A', is_active=False)

        def fake(stock, full, sources):
            if stock.code == '000001':
                raise RuntimeError('boom')
            return 0

        with patch('stocks.management.commands.fetch_stock_data.fetch_daily_data', side_effect=fake) as daily:
            out, _ = _run('fetch_stock_data', '--all', '--full-daily')
        self.assertEqual(daily.call_count, 2)  # 只含 is_active
        self.assertIsNone(daily.call_args.kwargs['sources'])
        self.assertIn('000001: 失败 - boom', out)
        self.assertIn('600000: 全量重拉 0 条日K线', out)

    def test_lock_busy_does_nothing(self):
        with patch('stocks.management.commands.fetch_stock_data.task_lock') as tl, \
                patch('stocks.management.commands.fetch_stock_data.fetch_stock_all_data') as one:
            tl.return_value.__enter__.return_value = False
            out, _ = _run('fetch_stock_data', '600000')
        self.assertIn('任务锁被占用', out)
        one.assert_not_called()


class RunSchedulerCommandTests(TestCase):
    def test_installs_signal_handlers_and_clamps_retry(self):
        handlers = {}

        def fake_run_forever(stop_event, lock_retry_seconds):
            self.assertEqual(lock_retry_seconds, 1)
            handlers[signal.SIGTERM](signal.SIGTERM, None)  # 模拟 systemd stop
            self.assertTrue(stop_event.is_set())
            return True

        with patch.dict('os.environ', {'STOCKTRACE_SCHEDULER': '1'}), \
                patch('signal.signal', side_effect=lambda s, h: handlers.__setitem__(s, h)), \
                patch('stocks.scheduler.run_forever', side_effect=fake_run_forever) as rf:
            _run('run_scheduler', '--lock-retry', '0')
        rf.assert_called_once()
        self.assertEqual(set(handlers), {signal.SIGTERM, signal.SIGINT})


class RunDetectorScanCommandTests(TestCase):
    def test_lock_busy_and_success(self):
        with patch('stocks.task_lock.task_lock') as tl, \
                patch('stocks.detector.scan.run_scan') as scan:
            tl.return_value.__enter__.return_value = False
            _, err = _run('run_detector_scan')
        self.assertIn('任务锁被占用', err)
        scan.assert_not_called()
        with patch('stocks.task_lock.task_lock') as tl, \
                patch('stocks.detector.scan.run_scan',
                      return_value={'date': '2026-10-09', 'signals': 0}):
            tl.return_value.__enter__.return_value = True
            out, _ = _run('run_detector_scan')
        self.assertIn('"date": "2026-10-09"', out)
