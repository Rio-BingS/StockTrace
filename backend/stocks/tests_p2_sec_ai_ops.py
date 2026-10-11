"""P2 修复回归测试：安全 / AI / 运维方向（sec-ai-ops）。"""
from unittest import mock

from django.test import TestCase
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
        with mock.patch('stocks.views.get_market_overview', side_effect=RuntimeError(secret)), \
                self.assertLogs('stocks.views', level='ERROR') as logs:
            resp = self.client.get('/api/market/')
        self._assert_generic(resp, secret)
        # 详情与 error_id 一起进日志，便于按编号定位
        self.assertTrue(any(resp.json()['error_id'] in line for line in logs.output))

    def test_bad_request_wrapper_hides_exception_text(self):
        secret = 'Traceback /opt/stocktrace/secret.py'
        with mock.patch('stocks.views.get_sector_rotation', side_effect=KeyError(secret)), \
                self.assertLogs('stocks.views', level='ERROR'):
            resp = self.client.get('/api/market/sectors/')
        self._assert_generic(resp, secret)

    def test_institutions_hides_exception_text(self):
        secret = 'upstream body <html>'
        with mock.patch('stocks.views.get_institution_holdings', side_effect=RuntimeError(secret)), \
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
        patcher = mock.patch('stocks.views._initial_fetch', return_value={'status': 'started'})
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
        with mock.patch('stocks.views.fetch_stock_margin') as margin, \
                mock.patch('stocks.views.get_etf_detail') as etf:
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
        from stocks.ai.throttle import _inflight
        from stocks.models import AiCallLog
        self.post.side_effect = RuntimeError('boom')
        c = APIClient(raise_request_exception=False)
        r = c.post(f'/api/stocks/{self.stock.id}/ai-analysis/')
        self.assertEqual(r.status_code, 500)
        self.assertFalse(_inflight)
        self.assertFalse(AiCallLog.objects.get().success)


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
        self.assertIn('latency_ms=', line)
        self.assertIn('error=auth HTTP 401', line)
        self.assertNotIn('secret-body', line)

    def test_fields_filled_when_migrated(self):
        from stocks.ai.throttle import _has_field
        from stocks.models import AiCallLog
        from stocks.tests_ai import _fake_response
        if not _has_field('latency_ms'):
            self.skipTest('AiCallLog 尚未迁移 model/latency_ms/error/status 字段（待 data 方向）')
        self.post.return_value = _fake_response(401, text='x')
        self.client.post(f'/api/stocks/{self.stock.id}/ai-analysis/')
        log = AiCallLog.objects.get()
        self.assertEqual(log.model, 'm')
        self.assertIsNotNone(log.latency_ms)
        self.assertEqual(log.error, 'auth HTTP 401')
        self.assertEqual(log.status, 'failed')
