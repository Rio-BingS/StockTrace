"""P2 修复回归测试：安全 / AI / 运维方向（sec-ai-ops）。"""
from unittest import mock

from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient


class UpstreamErrorNoEchoTests(TestCase):
    """SEC-07：行情端点失败只返回通用文案与 error_id，不回显异常原文。"""

    def setUp(self):
        self.client = APIClient()

    def _assert_generic(self, resp, secret):
        self.assertEqual(resp.status_code, 502)
        body = resp.json()
        self.assertNotIn(secret, body['detail'])
        self.assertTrue(body.get('error_id'))
        self.assertIn(body['error_id'], body['detail'])

    def test_market_overview_hides_exception_text(self):
        secret = 'http://10.0.0.5/internal 上游原文'
        with mock.patch('stocks.views.market.get_market_overview', side_effect=RuntimeError(secret)), \
                self.assertLogs('stocks.views', level='ERROR') as logs:
            resp = self.client.get('/api/market/')
        self._assert_generic(resp, secret)
        # 详情与 error_id 一起进日志，便于按编号定位
        self.assertTrue(any(resp.json()['error_id'] in line for line in logs.output))

    def test_bad_request_wrapper_hides_exception_text(self):
        secret = 'Traceback /opt/stocktrace/secret.py'
        with mock.patch('stocks.views.market.get_sector_rotation', side_effect=KeyError(secret)), \
                self.assertLogs('stocks.views', level='ERROR'):
            resp = self.client.get('/api/market/sectors/')
        self._assert_generic(resp, secret)

    def test_institutions_hides_exception_text(self):
        secret = 'upstream body <html>'
        with mock.patch('stocks.views.market.get_institution_holdings', side_effect=RuntimeError(secret)), \
                self.assertLogs('stocks.views', level='ERROR'):
            resp = self.client.get('/api/market/institutions/')
        self._assert_generic(resp, secret)

    def test_national_etf_flow_unavailable_message_hides_reason(self):
        from stocks.market import etf_flow
        data = etf_flow._unavailable_payload('k', 60, '1m', None, 'HTTPConnectionPool(host=10.1.1.1)')
        self.assertNotIn('10.1.1.1', data['message'])


class StockCodeValidationTests(TestCase):
    """SEC-08：股票代码格式校验；软删除恢复分支走序列化器。"""

    def setUp(self):
        self.client = APIClient()
        patcher = mock.patch('stocks.views.watchlist._initial_fetch', return_value={'status': 'started'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_create_rejects_bad_code(self):
        for bad in ('abc', '60000', '6000001', '60000x', '../etc'):
            resp = self.client.post('/api/stocks/', {'code': bad}, format='json')
            self.assertEqual(resp.status_code, 400, bad)
        from stocks.models import Stock
        self.assertFalse(Stock.objects.exists())

    def test_create_strips_whitespace(self):
        resp = self.client.post('/api/stocks/', {'code': ' 600000 '}, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['code'], '600000')

    def test_restore_goes_through_serializer(self):
        from stocks.models import Stock
        stock = Stock.objects.create(code='600000', name='浦发银行', is_active=False)
        # 非法字段在恢复分支同样被拒绝，且不会把股票恢复
        resp = self.client.post('/api/stocks/', {'code': '600000', 'name': 'x' * 51}, format='json')
        self.assertEqual(resp.status_code, 400)
        stock.refresh_from_db()
        self.assertFalse(stock.is_active)
        resp = self.client.post('/api/stocks/', {'code': ' 600000', 'quantity': 100}, format='json')
        self.assertEqual(resp.status_code, 201)
        stock.refresh_from_db()
        self.assertTrue(stock.is_active)
        self.assertEqual(stock.name, '600000')
        self.assertEqual(stock.quantity, 100)

    def test_update_rejects_bad_code(self):
        from stocks.models import Stock
        stock = Stock.objects.create(code='600000', name='浦发银行')
        resp = self.client.patch(f'/api/stocks/{stock.pk}/', {'code': 'zzz'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_margin_and_etf_routes_validate_code(self):
        with mock.patch('stocks.views.market.fetch_stock_margin') as margin, \
                mock.patch('stocks.views.market.get_etf_detail') as etf:
            self.assertEqual(self.client.get('/api/market/stock-margin/abc/').status_code, 400)
            self.assertEqual(self.client.get('/api/market/etfs/51030/').status_code, 400)
            margin.assert_not_called()
            etf.assert_not_called()
            margin.return_value = {'ok': 1}
            self.assertEqual(self.client.get('/api/market/stock-margin/600000/').status_code, 200)
            margin.assert_called_once_with('600000')


def _ai_fixtures():
    from datetime import date
    from decimal import Decimal
    from stocks.ai.crypto import encrypt_api_key
    from stocks.models import AiProvider, DailyQuote, Stock
    provider = AiProvider.objects.create(
        name='P', base_url='https://mock.local', model='m',
        api_key_encrypted=encrypt_api_key('sk-p2-0000'),
    )
    stock = Stock.objects.create(code='000001', name='测试')
    DailyQuote.objects.create(
        stock=stock, trade_date=date(2026, 9, 4),
        open_price=Decimal('10'), close_price=Decimal('10.5'),
        high_price=Decimal('10.8'), low_price=Decimal('9.9'),
        open_close_diff=Decimal('0.5'), open_close_pct=Decimal('5'),
        high_low_diff=Decimal('0.9'), high_low_pct=Decimal('9'),
        change_pct=Decimal('5'), volume=1, turnover=Decimal('1'),
    )
    return provider, stock


def _ai_patches(test):
    """断网桩：DNS 解析为公网地址，requests.post 由用例设置返回值。"""
    from stocks.tests_ai import _PUBLIC_DNS, _fake_getaddrinfo
    dns = mock.patch('stocks.ai.llm.socket.getaddrinfo',
                     side_effect=_fake_getaddrinfo(_PUBLIC_DNS))
    dns.start()
    test.addCleanup(dns.stop)
    sleep = mock.patch('stocks.ai.llm.time.sleep')
    sleep.start()
    test.addCleanup(sleep.stop)
    post = mock.patch('stocks.ai.llm.requests.post')
    test.addCleanup(post.stop)
    return post.start()


class AiThrottleAtomicTests(TestCase):
    """AIX-06：检查与占位同一事务；失败不计冷却；参数调用时读取。"""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.provider, self.stock = _ai_fixtures()
        self.post = _ai_patches(self)
        self.client = APIClient()

    def test_pending_reservation_blocks_concurrent_same_stock(self):
        from stocks.ai.throttle import cancel_call, check_throttle, reserve_call
        log, reason = reserve_call('analysis', provider=self.provider, stock=self.stock)
        self.assertIsNotNone(log)
        # 占位尚未完成：同股第二个请求被冷却挡住
        self.assertFalse(check_throttle('analysis', stock=self.stock)[0])
        second, reason = reserve_call('analysis', provider=self.provider, stock=self.stock)
        self.assertIsNone(second)
        self.assertIn('秒内', reason)
        cancel_call(log)
        self.assertTrue(check_throttle('analysis', stock=self.stock)[0])

    def test_failed_call_does_not_trigger_cooldown(self):
        from stocks.models import AiCallLog
        from stocks.tests_ai import _fake_response
        self.post.return_value = _fake_response(401, text='no')
        r1 = self.client.post(f'/api/stocks/{self.stock.id}/ai-analysis/')
        self.assertEqual(r1.status_code, 502)
        log = AiCallLog.objects.get()
        self.assertFalse(log.success)
        # 失败后立刻重试不应被"60 秒冷却"挡住
        r2 = self.client.post(f'/api/stocks/{self.stock.id}/ai-analysis/')
        self.assertEqual(r2.status_code, 502)
        self.assertEqual(AiCallLog.objects.count(), 2)

    def test_success_triggers_cooldown(self):
        from stocks.tests_p1_ai import _ok
        self.post.return_value = _ok('分析')
        self.assertEqual(self.client.post(f'/api/stocks/{self.stock.id}/ai-analysis/').status_code, 200)
        r2 = self.client.post(f'/api/stocks/{self.stock.id}/ai-analysis/')
        self.assertEqual(r2.status_code, 429)

    def test_limits_read_at_call_time(self):
        from django.test import override_settings
        from stocks.ai.throttle import check_throttle, log_call
        log_call('screener_comment')
        with override_settings(STOCKTRACE_AI_DAILY_LIMIT=1):
            allowed, reason = check_throttle('screener_comment')
        self.assertFalse(allowed)
        self.assertIn('1 次', reason)
        self.assertTrue(check_throttle('screener_comment')[0])
        log_call('analysis', stock=self.stock)
        with override_settings(STOCKTRACE_AI_STOCK_COOLDOWN=0):
            self.assertTrue(check_throttle('analysis', stock=self.stock)[0])

    def test_unexpected_exception_releases_reservation(self):
        from stocks.models import AiCallLog
        self.post.side_effect = RuntimeError('boom')
        c = APIClient(raise_request_exception=False)
        r = c.post(f'/api/stocks/{self.stock.id}/ai-analysis/')
        self.assertEqual(r.status_code, 500)
        log = AiCallLog.objects.get()
        self.assertFalse(log.success)
        self.assertEqual(log.status, AiCallLog.STATUS_FAILED)

    def test_pending_status_visible_across_processes(self):
        # 占位只落在数据库（无进程内状态）：另一个 worker 写下的 pending 同样挡冷却
        from stocks.ai.throttle import check_throttle
        from stocks.models import AiCallLog
        AiCallLog.objects.create(purpose='analysis', provider=self.provider, stock=self.stock,
                                 success=False, status=AiCallLog.STATUS_PENDING)
        self.assertFalse(check_throttle('analysis', stock=self.stock)[0])
        AiCallLog.objects.update(status=AiCallLog.STATUS_FAILED)
        self.assertTrue(check_throttle('analysis', stock=self.stock)[0])


class AiCallLogObservabilityTests(TestCase):
    """AIX-08：流水回填模型、耗时、错误原因（字段迁移前写 INFO 日志）。"""

    def setUp(self):
        self.provider, self.stock = _ai_fixtures()
        self.post = _ai_patches(self)
        self.client = APIClient()

    def test_error_text_has_no_upstream_body(self):
        from stocks.ai.llm import LlmError
        from stocks.ai.throttle import error_text
        self.assertEqual(error_text(LlmError('上游原文 secret', kind='auth', status_code=401)),
                         'auth HTTP 401')
        self.assertEqual(error_text(ValueError('x')), 'config')
        self.assertEqual(error_text(RuntimeError('y' * 500)), 'RuntimeError')
        self.assertEqual(error_text(None), '')

    def test_failed_call_logged_with_model_latency_error(self):
        from stocks.tests_ai import _fake_response
        self.post.return_value = _fake_response(401, text='secret-body')
        with self.assertLogs('stocks.ai.throttle', level='INFO') as logs:
            self.client.post(f'/api/stocks/{self.stock.id}/ai-analysis/')
        line = '\n'.join(logs.output)
        self.assertIn('model=m', line)
        self.assertIn('duration_ms=', line)
        self.assertIn('error=auth HTTP 401', line)
        self.assertNotIn('secret-body', line)

    def test_fields_filled_when_migrated(self):
        from stocks.models import AiCallLog
        from stocks.tests_ai import _fake_response
        self.post.return_value = _fake_response(401, text='x')
        self.client.post(f'/api/stocks/{self.stock.id}/ai-analysis/')
        log = AiCallLog.objects.get()
        self.assertEqual(log.model, 'm')
        self.assertIsNotNone(log.duration_ms)
        self.assertEqual(log.error_reason, 'auth HTTP 401')
        self.assertEqual(log.status, AiCallLog.STATUS_FAILED)
        self.assertIsNone(log.total_tokens)

    def test_success_fills_tokens_and_clears_error(self):
        from stocks.models import AiCallLog
        from stocks.tests_p1_ai import _ok
        self.post.return_value = _ok('分析')
        self.client.post(f'/api/stocks/{self.stock.id}/ai-analysis/')
        log = AiCallLog.objects.get()
        self.assertEqual(log.status, AiCallLog.STATUS_SUCCESS)
        self.assertIsNone(log.error_reason)
        self.assertEqual((log.prompt_tokens, log.completion_tokens, log.total_tokens), (3, 2, 5))


class AiMultiProviderTests(TestCase):
    """AIX-07：按顺序回退、推理模型参数、列表顺序与选择一致。"""

    def setUp(self):
        from stocks.ai.crypto import encrypt_api_key
        from stocks.models import AiProvider
        self.p1, self.stock = _ai_fixtures()
        self.p2 = AiProvider.objects.create(
            name='P2', base_url='https://b.com', model='m2',
            api_key_encrypted=encrypt_api_key('sk-p2-1111'),
        )
        self.post = _ai_patches(self)
        self.client = APIClient()

    def _urls(self):
        return [c.args[0] for c in self.post.call_args_list]

    def test_fallback_on_auth_error(self):
        from stocks.models import AiCallLog
        from stocks.tests_ai import _fake_response
        from stocks.tests_p1_ai import _ok
        self.post.side_effect = [_fake_response(401, text='no'), _ok('第二家')]
        r = self.client.post(f'/api/stocks/{self.stock.id}/ai-analysis/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['analysis'], '第二家')
        self.assertEqual(self._urls(), ['https://mock.local/chat/completions',
                                        'https://b.com/chat/completions'])
        # 一个请求只占一条流水，记在实际成功的服务商上
        log = AiCallLog.objects.get()
        self.assertTrue(log.success)
        self.assertEqual(log.provider_id, self.p2.id)

    def test_no_fallback_on_charged_errors(self):
        from stocks.tests_p1_ai import _ok
        self.post.return_value = _ok('半截', finish_reason='length')
        r = self.client.post(f'/api/stocks/{self.stock.id}/ai-analysis/')
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.data['error'], 'truncated')
        self.assertEqual(self.post.call_count, 1)

    def test_disabled_and_order_consistent_with_list(self):
        from stocks.tests_p1_ai import _ok
        self.p1.is_enabled = False
        self.p1.save()
        self.post.return_value = _ok('ok')
        self.client.post('/api/screener/ai/comment/', {'query': '', 'results': [{'code': '000001'}]},
                         format='json')
        self.assertEqual(self._urls(), ['https://b.com/chat/completions'])
        body = self.client.get('/api/ai-providers/').json()
        rows = body['results'] if isinstance(body, dict) else body
        listed = [p['id'] for p in rows]
        self.assertEqual(listed[0], self.p2.id)

    def test_reasoning_param_styles(self):
        from types import SimpleNamespace
        from stocks.ai.llm import build_payload, param_style
        cases = {
            'deepseek-chat': 'standard', 'gpt-4o': 'standard', 'qwen-max': 'standard',
            'deepseek-reasoner': 'reasoning', 'deepseek-r1': 'reasoning', 'qwq-32b': 'reasoning',
            'o1-mini': 'openai_reasoning', 'o3': 'openai_reasoning', 'gpt-5': 'openai_reasoning',
            'openai/o4-mini': 'openai_reasoning',
        }
        for model, style in cases.items():
            self.assertEqual(param_style(SimpleNamespace(model=model)), style, model)
        p = build_payload(SimpleNamespace(model='o1'), [], 0.3, 100)
        self.assertNotIn('temperature', p)
        self.assertNotIn('max_tokens', p)
        self.assertEqual(p['max_completion_tokens'], 100)
        p = build_payload(SimpleNamespace(model='deepseek-reasoner'), [], 0.3, 100)
        self.assertNotIn('temperature', p)
        self.assertEqual(p['max_tokens'], 100)
        p = build_payload(SimpleNamespace(model='deepseek-chat'), [], 0.3, 100)
        self.assertEqual((p['temperature'], p['max_tokens']), (0.3, 100))
        # 显式配置优先于模型名推断
        self.assertEqual(param_style(SimpleNamespace(model='o1', param_style='standard')), 'standard')


class AiAnalysisFinalQuotesTests(TestCase):
    """P1 遗留：个股 AI 分析只读取已收盘日线（与 DAT-13 读取侧一致）。"""

    def test_context_skips_intraday_bar(self):
        from datetime import date
        from decimal import Decimal
        from stocks.ai.prompts import build_stock_context
        from stocks.models import DailyQuote
        _, stock = _ai_fixtures()
        DailyQuote.objects.create(
            stock=stock, trade_date=date(2026, 9, 7), is_final=False,
            open_price=Decimal('10.5'), close_price=Decimal('11.11'),
            high_price=Decimal('11.2'), low_price=Decimal('10.4'),
            open_close_diff=Decimal('0.6'), open_close_pct=Decimal('5'),
            high_low_diff=Decimal('0.8'), high_low_pct=Decimal('7'),
            change_pct=Decimal('5.8'), volume=1, turnover=Decimal('1'),
        )
        text = build_stock_context(stock)
        self.assertIn('最新交易日：2026-09-04', text)
        self.assertNotIn('2026-09-07', text)
        self.assertNotIn('11.11', text)

    def test_context_volume_unit_is_shares(self):
        # DailyQuote.volume 入库单位是「股」（DAT-03），提示词不能再写「手」
        from stocks.ai.prompts import build_stock_context
        _, stock = _ai_fixtures()
        text = build_stock_context(stock)
        self.assertIn('成交量', text)
        self.assertNotIn('手', text.split('成交量', 1)[1].split('，', 1)[0])
        self.assertIn('股', text.split('成交量', 1)[1].split('，', 1)[0])


class SchedulerStepIsolationTests(TestCase):
    """REL-08：任务链逐步容错；探测器按快照依赖延后重试。"""

    def test_daily_summary_failure_does_not_skip_later_steps(self):
        from stocks import scheduler
        calls = []
        with mock.patch('stocks.jobs.fetch_daily_summary', side_effect=RuntimeError('x')), \
                mock.patch.object(scheduler, '_evaluate_alerts', lambda: calls.append('alerts')), \
                mock.patch.object(scheduler, '_cleanup_ai_logs', lambda: calls.append('ai')), \
                mock.patch.object(scheduler, '_cleanup_market_snapshots',
                                  side_effect=lambda: calls.append('snap')), \
                self.assertLogs('stocks.scheduler', level='ERROR') as logs:
            failed = scheduler._daily_summary_job()
        self.assertEqual(calls, ['alerts', 'ai', 'snap'])
        self.assertEqual(failed, ['收盘日线汇总'])
        self.assertTrue(any('Traceback' in line for line in logs.output))

    def test_intraday_failure_still_evaluates_alerts(self):
        from stocks import scheduler
        calls = []
        with mock.patch('stocks.jobs.fetch_intraday_update', side_effect=RuntimeError('x')), \
                mock.patch.object(scheduler, '_evaluate_alerts', lambda: calls.append('alerts')), \
                self.assertLogs('stocks.scheduler', level='ERROR'):
            scheduler._intraday_job()
        self.assertEqual(calls, ['alerts'])

    def _at(self, hh, mm):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime(2026, 9, 7, hh, mm, tzinfo=ZoneInfo('Asia/Shanghai'))

    def _run_detector(self, hh, mm):
        from stocks import scheduler
        with mock.patch('stocks.trading_calendar.is_trading_day', return_value=True), \
                mock.patch('stocks.scheduler.timezone.localtime', return_value=self._at(hh, mm)), \
                mock.patch('stocks.detector.scan.run_scan') as run_scan:
            scheduler._detector_scan_job()
        return run_scan

    def test_detector_defers_until_snapshots_ready(self):
        from datetime import date
        from stocks.models import DetectorRun, MarketDailySnapshot
        with self.assertLogs('stocks.scheduler', level='WARNING'):
            self.assertFalse(self._run_detector(17, 15).called)
        for kind in ('industry_ff', 'concept_ff'):  # #14：不再等 market_ff
            MarketDailySnapshot.objects.create(kind=kind, trade_date=date(2026, 9, 7), payload={})
        self.assertTrue(self._run_detector(17, 45).called)
        # 当日已扫过：后续重试点直接跳过
        DetectorRun.objects.create(trade_date=date(2026, 9, 7), status='ok')
        self.assertFalse(self._run_detector(18, 15).called)

    def test_detector_last_attempt_runs_degraded(self):
        with self.assertLogs('stocks.scheduler', level='WARNING'):
            self.assertTrue(self._run_detector(18, 45).called)

    def test_detector_cron_has_retry_slots(self):
        from apscheduler.schedulers.background import BackgroundScheduler
        from stocks.scheduler import register_jobs
        sched = register_jobs(BackgroundScheduler())
        trigger = str(sched.get_job('detector_scan').trigger)
        self.assertIn("hour='17-18'", trigger)
        self.assertIn("minute='15,45'", trigger)


class FetchStockDataCommandTests(TestCase):
    """REL-09：--all 传递 --days；失败/跳过/参数错误时非零退出。"""

    CMD = 'stocks.management.commands.fetch_stock_data'

    def setUp(self):
        from stocks.models import Stock
        Stock.objects.create(code='600000', name='A')
        Stock.objects.create(code='600001', name='B')
        sleep = mock.patch(f'{self.CMD}.time.sleep')
        sleep.start()
        self.addCleanup(sleep.stop)

    def _call(self, *args):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command('fetch_stock_data', *args, stdout=out)
        return out.getvalue()

    def test_all_passes_days(self):
        with mock.patch(f'{self.CMD}.fetch_stock_all_data', return_value=3) as f:
            self._call('--all', '--days', '4')
        self.assertEqual(f.call_count, 2)
        for call in f.call_args_list:
            self.assertEqual(call.kwargs, {'days_back': 4})

    def test_all_without_days_stays_light(self):
        with mock.patch(f'{self.CMD}.fetch_stock_all_data', return_value=3) as f:
            self._call('--all')
        self.assertTrue(all(c.kwargs == {'light': True} for c in f.call_args_list))

    def test_failures_exit_nonzero(self):
        from django.core.management import CommandError
        with mock.patch(f'{self.CMD}.fetch_stock_all_data', side_effect=[3, RuntimeError('x')]), \
                self.assertRaises(CommandError) as ctx:
            self._call('--all')
        self.assertIn('1 只', str(ctx.exception))
        with mock.patch(f'{self.CMD}.fetch_daily_data', return_value=0), \
                self.assertRaises(CommandError):
            self._call('600000', '--full-daily')

    def test_bad_args_exit_nonzero(self):
        from django.core.management import CommandError
        for args in (('--days', '9', '--all'), ('999999',), (), ('--all', '--source', 'sina')):
            with self.assertRaises(CommandError, msg=args):
                self._call(*args)


class GunicornConfigTests(TestCase):
    """REL-10：只有一份 gunicorn 配置；显式关闭 preload；无效配置已删除。"""

    def test_single_config_without_preload(self):
        import runpy
        from pathlib import Path
        from django.conf import settings
        backend = Path(settings.BASE_DIR)
        self.assertFalse((backend.parent / 'deploy' / 'gunicorn_config.py').exists())
        conf = runpy.run_path(str(backend / 'gunicorn_config.py'))
        self.assertIs(conf['preload_app'], False)
        self.assertEqual(Path(conf['chdir']), backend)
        self.assertEqual(conf['wsgi_app'], 'StockTrace.wsgi:application')

    def test_scheduler_autostart_removed(self):
        from django.conf import settings
        self.assertFalse(hasattr(settings, 'SCHEDULER_AUTOSTART'))


class RuntimeVersionDeclTests(TestCase):
    """OPS-07：运行时版本声明与 STATIC_ROOT 可配置。"""

    def test_runtime_versions_declared(self):
        import json
        from pathlib import Path
        from django.conf import settings
        root = Path(settings.BASE_DIR).parent
        self.assertEqual((root / '.python-version').read_text().strip(), '3.12')
        self.assertEqual((root / 'frontend' / '.nvmrc').read_text().strip(), '22')
        pkg = json.loads((root / 'frontend' / 'package.json').read_text(encoding='utf-8'))
        self.assertIn('22.13', pkg['engines']['node'])

    def test_static_root_from_env(self):
        import os
        import subprocess
        import sys
        from pathlib import Path
        from django.conf import settings
        self.assertEqual(Path(settings.STATIC_ROOT), Path(settings.BASE_DIR).parent / 'staticfiles')
        env = dict(os.environ, STOCKTRACE_STATIC_ROOT='/srv/st-static',
                   DJANGO_SETTINGS_MODULE='StockTrace.settings')
        out = subprocess.run(
            [sys.executable, '-c', 'import django;django.setup();'
             'from django.conf import settings;print(settings.STATIC_ROOT)'],
            cwd=settings.BASE_DIR, env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(out.stdout.strip(), '/srv/st-static', out.stderr[-500:])


class AiAsyncJobTests(TestCase):
    """P1 遗留：个股 AI 分析异步化（202 + job_id + 轮询），默认仍同步。"""

    def setUp(self):
        from stocks.ai import jobs
        self.provider, self.stock = _ai_fixtures()
        self.post = _ai_patches(self)
        self.client = APIClient()
        # 任务已落库（AiJob）：不起后台线程，用 run_pending() 在当前线程执行，
        # TestCase 事务内的数据对它可见
        patcher = mock.patch.object(jobs, 'ensure_worker')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.run_all = jobs.run_pending

    def _url(self):
        return f'/api/stocks/{self.stock.id}/ai-analysis/'

    def test_async_returns_job_and_result_via_polling(self):
        from stocks.models import AiCallLog
        from stocks.tests_p1_ai import _ok
        self.post.return_value = _ok('异步分析')
        r = self.client.post(self._url() + '?async=1')
        self.assertEqual(r.status_code, 202)
        job_id = r.data['job_id']
        self.assertEqual(r.data['status'], 'queued')
        self.post.assert_not_called()
        # 额度在提交时就已预占：同股立即再发被冷却挡住
        self.assertEqual(AiCallLog.objects.count(), 1)
        self.assertEqual(self.client.post(self._url() + '?async=1').status_code, 429)
        poll = self.client.get(f'/api/ai/jobs/{job_id}/')
        self.assertEqual(poll.data['status'], 'queued')
        self.assertEqual(poll.data['queued_ahead'], 0)
        self.run_all()
        poll = self.client.get(f'/api/ai/jobs/{job_id}/')
        self.assertEqual(poll.data['status'], 'done')
        self.assertEqual(poll.data['http_status'], 200)
        self.assertEqual(poll.data['result']['analysis'], '异步分析')
        self.assertTrue(poll.data['result']['disclaimer'])
        self.assertTrue(AiCallLog.objects.get().success)

    def test_async_failure_reported_in_job(self):
        from stocks.tests_ai import _fake_response
        self.post.return_value = _fake_response(401, text='secret')
        r = self.client.post(self._url(), {'async': True}, format='json')
        self.assertEqual(r.status_code, 202)
        self.run_all()
        poll = self.client.get(f"/api/ai/jobs/{r.data['job_id']}/")
        self.assertEqual(poll.data['status'], 'failed')
        self.assertEqual(poll.data['http_status'], 502)
        self.assertEqual(poll.data['result']['error'], 'auth')
        self.assertNotIn('secret', str(poll.data))

    def test_job_waits_for_slot_instead_of_busy(self):
        from stocks.ai import llm
        from stocks.tests_p1_ai import _ok
        self.post.return_value = _ok('ok')
        # 后台任务持有名额时，内部 chat 不再抢名额；同步请求照旧立即 429
        with llm.hold_ai_slot(1):
            text, _ = llm.LlmClient(self.provider).chat([{'role': 'user', 'content': 'x'}])
            self.assertEqual(text, 'ok')
        with llm._AiSlot():
            r = self.client.post(self._url())
            self.assertEqual(r.status_code, 429)
            with self.assertRaises(llm.LlmError) as ctx:
                with llm.hold_ai_slot(0.01):
                    pass
            self.assertEqual(ctx.exception.kind, 'busy')

    def test_unknown_job_404_and_sync_default(self):
        from stocks.tests_p1_ai import _ok
        self.assertEqual(self.client.get('/api/ai/jobs/nope/').status_code, 404)
        self.post.return_value = _ok('同步')
        r = self.client.post(self._url())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['analysis'], '同步')

    def test_finished_jobs_pruned(self):
        from datetime import timedelta
        from django.utils import timezone
        from stocks.ai import jobs
        from stocks.models import AiJob
        old = timezone.now() - timedelta(hours=2)
        AiJob.objects.create(job_id='old', kind='analysis', status='done',
                             http_status=200, result={}, finished_at=old)
        jobs.submit('analysis', {})
        self.assertIsNone(jobs.get('old'))
        self.assertFalse(AiJob.objects.filter(job_id='old').exists())


class AiAsyncJobThreadTests(TransactionTestCase):
    """真实后台线程跑一遍：结果可轮询，流水在后台线程里回填。"""

    def test_real_executor(self):
        from stocks.ai import jobs
        from stocks.models import AiCallLog
        from stocks.tests_p1_ai import _ok
        provider, stock = _ai_fixtures()
        post = _ai_patches(self)
        post.return_value = _ok('线程结果')
        # 空闲立即退出，用例结束时等线程收尾，不把轮询线程留给后面的用例
        for name, value in (('IDLE_EXIT_SECONDS', 0), ('POLL_SECONDS', 0.01)):
            patcher = mock.patch.object(jobs, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._join_worker, jobs)
        r = APIClient().post(f'/api/stocks/{stock.id}/ai-analysis/?async=1')
        self.assertEqual(r.status_code, 202)
        # 测试库是共享缓存的内存 SQLite（表级锁），主线程边轮询边读会和后台线程写冲突；
        # 改为等线程跑完空闲退出后再读结果（生产是 WAL 文件库，读写不互斥）
        self._join_worker(jobs)
        self.assertIsNone(jobs._worker_thread)
        job = jobs.get(r.data['job_id'])
        self.assertEqual(job['status'], 'done', job)
        self.assertEqual(job['result']['analysis'], '线程结果')
        self.assertTrue(AiCallLog.objects.get().success)

    @staticmethod
    def _join_worker(jobs, timeout=10):
        # 线程空闲退出时会把 _worker_thread 置空；可能已经退出，也可能还在跑
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            thread = jobs._worker_thread
            if thread is None or not thread.is_alive():
                return
            thread.join(0.1)
