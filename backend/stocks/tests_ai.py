"""AI 功能测试：Key 加密、Provider 序列化脱敏、节流、条件选股引擎、端点行为"""
import json
from datetime import date, timedelta
from decimal import Decimal
import socket
from unittest.mock import MagicMock, patch

import requests

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from .models import AiCallLog, AiProvider, DailyQuote, Stock
from .ai.crypto import decrypt_api_key, encrypt_api_key, mask_api_key
from .ai.llm import LlmClient, LlmError, validate_base_url
from .ai.screener import ConditionError, run_screener
from .ai.throttle import DAILY_LIMIT, check_throttle, log_call


def _no_valuation(codes):
    """估值快照（腾讯）是独立上游：选股用例按「估值不可用」口径运行，避免触网（OPS-03）"""
    return {}


class CryptoTests(TestCase):
    def test_roundtrip(self):
        token = encrypt_api_key('sk-test-1234')
        self.assertNotIn('sk-test-1234', token)
        self.assertEqual(decrypt_api_key(token), 'sk-test-1234')

    def test_mask_keeps_last_four(self):
        self.assertEqual(mask_api_key('sk-abcdef1234'), '****1234')
        self.assertEqual(mask_api_key('1234'), '****')


def _mock_chat_json(spec):
    """构造 LlmClient.chat_json 的 mock 返回（chat_json 已完成 JSON 解析）"""
    return lambda messages, **kwargs: (spec, {})


@patch('stocks.ai.valuation.fetch_valuation_map', new=_no_valuation)
class ScreenerEngineTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(code='000001', name='测试银行')
        DailyQuote.objects.create(
            stock=self.stock, trade_date=date(2026, 9, 4),
            open_price=Decimal('10.00'), close_price=Decimal('10.50'),
            high_price=Decimal('10.80'), low_price=Decimal('9.90'),
            open_close_diff=Decimal('0.50'), open_close_pct=Decimal('5.0000'),
            high_low_diff=Decimal('0.90'), high_low_pct=Decimal('9.0909'),
            prev_close=Decimal('10.00'), change_diff=Decimal('0.50'),
            change_pct=Decimal('5.0000'), volume=100000, turnover=Decimal('105000000'),
        )

    def test_condition_match(self):
        rows = run_screener({'logic': 'all', 'conditions': [
            {'field': 'change_pct', 'op': 'gt', 'value': 3},
        ]})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['code'], '000001')

    def test_condition_no_match(self):
        rows = run_screener({'logic': 'all', 'conditions': [
            {'field': 'change_pct', 'op': 'gt', 'value': 10},
        ]})
        self.assertEqual(rows, [])

    def test_between(self):
        rows = run_screener({'logic': 'all', 'conditions': [
            {'field': 'turnover', 'op': 'between', 'value': [50000000, 200000000]},
        ]})
        self.assertEqual(len(rows), 1)

    def test_any_logic(self):
        rows = run_screener({'logic': 'any', 'conditions': [
            {'field': 'change_pct', 'op': 'gt', 'value': 10},
            {'field': 'change_pct', 'op': 'lt', 'value': 1},
            {'field': 'change_pct', 'op': 'gt', 'value': 3},
        ]})
        self.assertEqual(len(rows), 1)

    def test_invalid_field_raises(self):
        with self.assertRaises(ConditionError):
            run_screener({'logic': 'all', 'conditions': [
                {'field': 'pe', 'op': 'gt', 'value': 10},
            ]})

    def test_inactive_stock_excluded(self):
        self.stock.is_active = False
        self.stock.save()
        rows = run_screener({'logic': 'all', 'conditions': [
            {'field': 'change_pct', 'op': 'gt', 'value': 0},
        ]})
        self.assertEqual(rows, [])

    def test_null_field_is_not_treated_as_zero(self):
        """涨跌幅为 null 的股票不应命中 change_pct 相关条件"""
        stock2 = Stock.objects.create(code='000002', name='无涨跌')
        DailyQuote.objects.create(
            stock=stock2, trade_date=date(2026, 9, 4),
            open_price=Decimal('10.00'), close_price=Decimal('10.00'),
            high_price=Decimal('10.00'), low_price=Decimal('10.00'),
            open_close_diff=Decimal('0'), open_close_pct=Decimal('0'),
            high_low_diff=Decimal('0'), high_low_pct=Decimal('0'),
            prev_close=None, change_diff=None, change_pct=None,
        )
        rows = run_screener({'logic': 'all', 'conditions': [
            {'field': 'change_pct', 'op': 'gt', 'value': -100},
        ]})
        self.assertEqual([r['code'] for r in rows], ['000001'])


class ThrottleTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(code='000001', name='测试')

    def test_cooldown_blocks_second_call(self):
        log_call('analysis', stock=self.stock)
        allowed, reason = check_throttle('analysis', stock=self.stock)
        self.assertFalse(allowed)
        self.assertIn('秒内', reason)

    def test_cooldown_expires(self):
        log_call('analysis', stock=self.stock)
        AiCallLog.objects.update(
            created_at=timezone.now() - timedelta(seconds=120)
        )
        allowed, _ = check_throttle('analysis', stock=self.stock)
        self.assertTrue(allowed)

    def test_daily_limit_blocks(self):
        for _ in range(DAILY_LIMIT):
            log_call('screener_comment')
        allowed, reason = check_throttle('screener_comment')
        self.assertFalse(allowed)
        self.assertIn('上限', reason)


def _fake_getaddrinfo(mapping):
    """构造 socket.getaddrinfo 替身：host -> IP；未登记的主机视为解析失败（测试断网）"""
    def _resolve(host, port, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror('not found')
        ip = mapping[host]
        family = socket.AF_INET6 if ':' in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, '', (ip, port))]
    return _resolve


# 测试用公网 IP（文档里常见的 example.com 地址），避免依赖真实 DNS
_PUBLIC_DNS = {
    'a.com': '93.184.216.34', 'b.com': '93.184.216.35',
    'api.deepseek.com': '93.184.216.36', 'mock.local': '93.184.216.37',
    'evil-internal.example': '10.0.0.5', 'meta.example': '169.254.169.254',
    'v6-loop.example': '::1',
}


class AiProviderApiTests(TestCase):
    def setUp(self):
        patcher = patch('stocks.ai.llm.socket.getaddrinfo',
                        side_effect=_fake_getaddrinfo(_PUBLIC_DNS))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _create(self, base_url='https://a.com', key='sk-old-1111'):
        response = self.client.post('/api/ai-providers/', {
            'name': 'A', 'base_url': base_url, 'model': 'm', 'api_key': key,
        }, content_type='application/json')
        self.assertEqual(response.status_code, 201, response.data)
        return AiProvider.objects.get(pk=response.data['id'])

    def test_create_masks_key_and_stores_encrypted(self):
        response = self.client.post('/api/ai-providers/', {
            'name': 'DeepSeek', 'base_url': 'https://api.deepseek.com',
            'model': 'deepseek-chat', 'api_key': 'sk-secret-9999',
        }, content_type='application/json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['api_key_masked'], '****9999')
        self.assertNotIn('api_key', response.data)
        provider = AiProvider.objects.get()
        self.assertNotIn('sk-secret-9999', provider.api_key_encrypted)

    def test_update_without_key_keeps_old(self):
        self.client.post('/api/ai-providers/', {
            'name': 'A', 'base_url': 'https://a.com', 'model': 'm',
            'api_key': 'sk-old-1111',
        }, content_type='application/json')
        pid = AiProvider.objects.get().id
        response = self.client.patch(
            f'/api/ai-providers/{pid}/', {'model': 'm2'},
            content_type='application/json')
        self.assertEqual(response.status_code, 200)
        provider = AiProvider.objects.get()
        self.assertEqual(decrypt_api_key(provider.api_key_encrypted), 'sk-old-1111')
        self.assertEqual(provider.model, 'm2')

    def test_update_with_same_base_url_without_key_ok(self):
        # 前端编辑表单总会带上原 base_url：未改动时不要求重新录入 Key
        provider = self._create()
        response = self.client.patch(
            f'/api/ai-providers/{provider.id}/',
            {'base_url': 'https://a.com/', 'name': 'A2'},
            content_type='application/json')
        self.assertEqual(response.status_code, 200, response.data)
        provider.refresh_from_db()
        self.assertEqual(decrypt_api_key(provider.api_key_encrypted), 'sk-old-1111')

    def test_update_base_url_without_key_rejected(self):
        # SEC-01：只改 base_url 不带 Key → 400，库里地址与 Key 都不变
        provider = self._create()
        response = self.client.patch(
            f'/api/ai-providers/{provider.id}/', {'base_url': 'https://b.com'},
            content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('api_key', response.data)
        provider.refresh_from_db()
        self.assertEqual(provider.base_url, 'https://a.com')
        self.assertEqual(decrypt_api_key(provider.api_key_encrypted), 'sk-old-1111')

    def test_update_base_url_with_new_key_ok(self):
        provider = self._create()
        response = self.client.patch(
            f'/api/ai-providers/{provider.id}/',
            {'base_url': 'https://b.com', 'api_key': 'sk-new-5555'},
            content_type='application/json')
        self.assertEqual(response.status_code, 200, response.data)
        provider.refresh_from_db()
        self.assertEqual(provider.base_url, 'https://b.com')
        self.assertEqual(decrypt_api_key(provider.api_key_encrypted), 'sk-new-5555')

    def test_insecure_base_urls_rejected(self):
        bad_urls = [
            'http://a.com',                      # 非 https
            'ftp://a.com',
            'https://127.0.0.1:8000',            # 回环
            'https://[::1]/v1',
            'https://10.1.2.3',                  # 私网
            'https://192.168.1.1',
            'https://169.254.169.254/latest',    # 链路本地（云元数据）
            'https://0.0.0.0',
            'https://[::ffff:127.0.0.1]',        # IPv4 映射回环
            'https://evil-internal.example',     # DNS 解析到私网
            'https://meta.example',              # DNS 解析到链路本地
            'https://v6-loop.example',           # DNS 解析到 ::1
            'https://unresolvable.example',      # 解析失败也拒绝
            'https://user:pw@a.com',
            'https://a.com/v1?x=1',
            'not a url',
        ]
        for url in bad_urls:
            with self.subTest(url=url):
                response = self.client.post('/api/ai-providers/', {
                    'name': 'X', 'base_url': url, 'model': 'm', 'api_key': 'sk-x-0000',
                }, content_type='application/json')
                self.assertEqual(response.status_code, 400)
                self.assertIn('base_url', response.data)
        self.assertFalse(AiProvider.objects.exists())

    def test_base_url_normalized_trailing_slash(self):
        provider = self._create(base_url='https://api.deepseek.com/v1/')
        self.assertEqual(provider.base_url, 'https://api.deepseek.com/v1')

    @override_settings(AI_ALLOW_HTTP_BASE_URL=True, AI_BASE_URL_ALLOWED_HOSTS=['127.0.0.1'])
    def test_local_dev_allowed_via_settings(self):
        provider = self._create(base_url='http://127.0.0.1:11434/v1')
        self.assertEqual(provider.base_url, 'http://127.0.0.1:11434/v1')

    @override_settings(AI_BASE_URL_ALLOWED_HOSTS=['api.deepseek.com'])
    def test_allowed_hosts_whitelist_enforced(self):
        self._create(base_url='https://api.deepseek.com')
        response = self.client.post('/api/ai-providers/', {
            'name': 'X', 'base_url': 'https://a.com', 'model': 'm', 'api_key': 'sk-x-0000',
        }, content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('base_url', response.data)

    def test_list_never_leaks_plaintext(self):
        self.client.post('/api/ai-providers/', {
            'name': 'A', 'base_url': 'https://a.com', 'model': 'm',
            'api_key': 'sk-plain-2222',
        }, content_type='application/json')
        response = self.client.get('/api/ai-providers/')
        body = json.dumps(response.json())
        self.assertNotIn('sk-plain-2222', body)
        self.assertIn('****2222', body)


@patch('stocks.ai.valuation.fetch_valuation_map', new=_no_valuation)
class AiEndpointTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(code='000001', name='测试')
        DailyQuote.objects.create(
            stock=self.stock, trade_date=date(2026, 9, 4),
            open_price=Decimal('10.00'), close_price=Decimal('10.50'),
            high_price=Decimal('10.80'), low_price=Decimal('9.90'),
            open_close_diff=Decimal('0.50'), open_close_pct=Decimal('5.0000'),
            high_low_diff=Decimal('0.90'), high_low_pct=Decimal('9.0909'),
            prev_close=Decimal('10.00'), change_diff=Decimal('0.50'),
            change_pct=Decimal('5.0000'), volume=100000, turnover=Decimal('105000000'),
        )
        self.provider = AiProvider.objects.create(
            name='Mock', base_url='https://mock.local', model='mock-1',
            api_key_encrypted=encrypt_api_key('sk-mock-3333'),
        )

    def test_analysis_requires_provider_configured(self):
        self.provider.is_enabled = False
        self.provider.save()
        response = self.client.post(f'/api/stocks/{self.stock.id}/ai-analysis/')
        self.assertEqual(response.status_code, 400)
        self.assertIn('未配置', response.data['detail'])

    @patch('stocks.ai.views.LlmClient')
    def test_analysis_success_with_disclaimer(self, mock_client_cls):
        mock_client_cls.return_value.provider = self.provider
        mock_client_cls.return_value.chat.return_value = ('看起来不错', {})
        response = self.client.post(f'/api/stocks/{self.stock.id}/ai-analysis/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['analysis'], '看起来不错')
        self.assertEqual(response.data['disclaimer'], 'AI 生成，非投资建议')

    @patch('stocks.ai.views.LlmClient')
    def test_analysis_upstream_error_is_honest(self, mock_client_cls):
        mock_client_cls.return_value.provider = self.provider
        mock_client_cls.return_value.chat.side_effect = LlmError('上游挂了')
        response = self.client.post(f'/api/stocks/{self.stock.id}/ai-analysis/')
        self.assertEqual(response.status_code, 502)
        self.assertIn('上游挂了', response.data['detail'])

    def test_screener_invalid_condition_returns_400(self):
        response = self.client.post('/api/screener/', {
            'logic': 'all', 'conditions': [{'field': 'pe', 'op': 'gt', 'value': 10}],
        }, content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_screener_success(self):
        response = self.client.post('/api/screener/', {
            'logic': 'all', 'conditions': [{'field': 'change_pct', 'op': 'gt', 'value': 3}],
        }, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)

    @patch('stocks.ai.views.LlmClient')
    def test_screener_ai_translates_and_executes(self, mock_client_cls):
        spec = {'logic': 'all', 'conditions': [
            {'field': 'change_pct', 'op': 'gt', 'value': 3},
        ]}
        mock_client_cls.return_value.provider = self.provider
        mock_client_cls.return_value.chat_json.side_effect = _mock_chat_json(spec)
        response = self.client.post('/api/screener/ai/', {
            'query': '涨3个点以上',
        }, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['conditions'], spec)

    @patch('stocks.ai.views.LlmClient')
    def test_screener_ai_rejects_bad_translation(self, mock_client_cls):
        bad_spec = {'logic': 'all', 'conditions': [
            {'field': '市盈率', 'op': 'gt', 'value': 10},
        ]}
        mock_client_cls.return_value.provider = self.provider
        mock_client_cls.return_value.chat_json.side_effect = _mock_chat_json(bad_spec)
        response = self.client.post('/api/screener/ai/', {
            'query': '低市盈率',
        }, content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('无法执行', response.data['detail'])

    @patch('stocks.ai.views.LlmClient')
    def test_comment_success_with_disclaimer(self, mock_client_cls):
        mock_client_cls.return_value.provider = self.provider
        mock_client_cls.return_value.chat.return_value = ('强势', {})
        rows = run_screener({'logic': 'all', 'conditions': [
            {'field': 'change_pct', 'op': 'gt', 'value': 3},
        ]})
        response = self.client.post('/api/screener/ai/comment/', {
            'query': '涨3个点以上', 'results': rows,
        }, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['comment'], '强势')
        self.assertEqual(response.data['disclaimer'], 'AI 生成，非投资建议')

    def test_comment_requires_results(self):
        response = self.client.post('/api/screener/ai/comment/', {
            'query': 'x', 'results': [],
        }, content_type='application/json')
        self.assertEqual(response.status_code, 400)


def _fake_response(status_code, text='', json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if json_data is None:
        resp.json.side_effect = ValueError('no json')
    else:
        resp.json.return_value = json_data
    return resp


# 关闭重试：这里只验证单次请求的安全行为与错误分类，重试见 tests_p1_ai.py
@override_settings(STOCKTRACE_LLM_MAX_RETRIES=0)
@patch('stocks.ai.llm.socket.getaddrinfo', side_effect=_fake_getaddrinfo(_PUBLIC_DNS))
class LlmClientSecurityTests(TestCase):
    """SEC-01：LlmClient 调用前再次校验地址、不跟随重定向"""

    def _provider(self, base_url='https://mock.local'):
        # 直接走 ORM 写库，模拟历史遗留的不合规地址（绕过序列化器校验）
        return AiProvider.objects.create(
            name='P', base_url=base_url, model='m',
            api_key_encrypted=encrypt_api_key('sk-leak-7777'),
        )

    @patch('stocks.ai.llm.requests.post')
    def test_redirect_not_followed(self, mock_post, _dns):
        mock_post.return_value = _fake_response(
            302, text='<a href="http://169.254.169.254/">moved</a>')
        with self.assertRaises(LlmError) as ctx:
            LlmClient(self._provider()).chat([{'role': 'user', 'content': 'x'}])
        self.assertEqual(ctx.exception.kind, 'redirect')
        self.assertEqual(ctx.exception.status_code, 302)
        self.assertIs(mock_post.call_args.kwargs['allow_redirects'], False)

    @patch('stocks.ai.llm.requests.post')
    def test_success_passes_allow_redirects_false(self, mock_post, _dns):
        mock_post.return_value = _fake_response(200, json_data={
            'choices': [{'message': {'content': 'ok'}}], 'usage': {}})
        text, _ = LlmClient(self._provider()).chat([{'role': 'user', 'content': 'x'}])
        self.assertEqual(text, 'ok')
        self.assertIs(mock_post.call_args.kwargs['allow_redirects'], False)

    @patch('stocks.ai.llm.requests.post')
    def test_legacy_internal_url_blocked_before_request(self, mock_post, _dns):
        for url in ('http://mock.local', 'https://127.0.0.1:9000',
                    'https://evil-internal.example'):
            with self.subTest(url=url):
                with self.assertRaises(LlmError) as ctx:
                    LlmClient(self._provider(url)).chat([{'role': 'user', 'content': 'x'}])
                self.assertEqual(ctx.exception.kind, 'config')
        mock_post.assert_not_called()

    @patch('stocks.ai.llm.requests.post')
    def test_error_kinds_classified(self, mock_post, _dns):
        cases = [(401, 'auth'), (403, 'auth'), (429, 'rate_limit'), (500, 'upstream')]
        for code, kind in cases:
            with self.subTest(code=code):
                mock_post.return_value = _fake_response(code, text='body')
                with self.assertRaises(LlmError) as ctx:
                    LlmClient(self._provider()).chat([{'role': 'user', 'content': 'x'}])
                self.assertEqual(ctx.exception.kind, kind)
                self.assertEqual(ctx.exception.status_code, code)
        mock_post.side_effect = requests.Timeout('slow')
        with self.assertRaises(LlmError) as ctx:
            LlmClient(self._provider()).chat([{'role': 'user', 'content': 'x'}])
        self.assertEqual(ctx.exception.kind, 'timeout')

    def test_validate_base_url_direct(self, _dns):
        self.assertEqual(validate_base_url(' https://a.com/v1/ '), 'https://a.com/v1')
        with self.assertRaises(ValueError):
            validate_base_url('https://100.64.0.1')  # 运营商级 NAT 共享地址
        with self.assertRaises(ValueError):
            validate_base_url('https://224.0.0.1')   # 组播


@patch('stocks.ai.llm.socket.getaddrinfo', side_effect=_fake_getaddrinfo(_PUBLIC_DNS))
class ProviderTestEndpointTests(TestCase):
    """SEC-01：test 端点不回显上游内容、纳入频控并记流水"""

    def setUp(self):
        self.provider = AiProvider.objects.create(
            name='P', base_url='https://mock.local', model='m',
            api_key_encrypted=encrypt_api_key('sk-test-8888'),
        )
        self.url = f'/api/ai-providers/{self.provider.id}/test/'

    @patch('stocks.ai.llm.requests.post')
    def test_upstream_body_not_echoed(self, mock_post, _dns):
        secret_body = 'root:x:0:0:SECRET-INTERNAL-CONTENT'
        mock_post.return_value = _fake_response(500, text=secret_body)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 502)
        body = json.dumps(response.json(), ensure_ascii=False)
        self.assertNotIn('SECRET-INTERNAL-CONTENT', body)
        self.assertEqual(response.data['error'], 'upstream')
        self.assertEqual(response.data['upstream_status'], 500)

    @patch('stocks.ai.llm.requests.post')
    def test_success_does_not_echo_reply(self, mock_post, _dns):
        mock_post.return_value = _fake_response(200, json_data={
            'choices': [{'message': {'content': 'LEAKED-REPLY'}}],
            'usage': {'prompt_tokens': 5, 'completion_tokens': 1}})
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'status': 'ok'})
        log = AiCallLog.objects.get(purpose='provider_test')
        self.assertTrue(log.success)
        self.assertEqual(log.prompt_tokens, 5)

    @patch('stocks.ai.llm.requests.post')
    def test_redirect_reported_generically(self, mock_post, _dns):
        mock_post.return_value = _fake_response(301, text='Location: http://10.0.0.1/')
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.data['error'], 'redirect')
        self.assertNotIn('10.0.0.1', json.dumps(response.json()))

    @patch('stocks.ai.llm.requests.post')
    def test_internal_base_url_blocked(self, mock_post, _dns):
        AiProvider.objects.filter(pk=self.provider.pk).update(
            base_url='http://169.254.169.254/latest/meta-data')
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.data['error'], 'config')
        mock_post.assert_not_called()

    @patch('stocks.ai.llm.requests.post')
    def test_rate_limited_and_logged(self, mock_post, _dns):
        mock_post.return_value = _fake_response(500, text='x')
        from .ai.throttle import PROVIDER_TEST_PER_MINUTE
        for _ in range(PROVIDER_TEST_PER_MINUTE):
            self.assertEqual(self.client.post(self.url).status_code, 502)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(mock_post.call_count, PROVIDER_TEST_PER_MINUTE)
        self.assertEqual(
            AiCallLog.objects.filter(purpose='provider_test', success=False).count(),
            PROVIDER_TEST_PER_MINUTE)
        # test 调用不占用正式 AI 功能的每日额度
        allowed, _ = check_throttle('screener_comment')
        self.assertTrue(allowed)


def _make_bars(stock, closes, volumes=None, start=date(2026, 6, 1)):
    """按收盘价序列（升序交易日）造日线；open/high/low 与收盘相同。"""
    for i, close in enumerate(closes):
        vol = volumes[i] if volumes else 100000
        DailyQuote.objects.create(
            stock=stock, trade_date=start + timedelta(days=i),
            open_price=Decimal(str(close)), close_price=Decimal(str(close)),
            high_price=Decimal(str(close)), low_price=Decimal(str(close)),
            open_close_diff=Decimal('0'), open_close_pct=Decimal('0'),
            high_low_diff=Decimal('0'), high_low_pct=Decimal('0'),
            volume=vol, turnover=Decimal('1000000'),
        )


@patch('stocks.ai.valuation.fetch_valuation_map', new=_no_valuation)
class ScreenerDerivedFieldsTests(TestCase):
    """多日衍生字段：区间涨跌/均线/量比/新高新低/连涨天数。"""

    def setUp(self):
        self.stock = Stock.objects.create(code='600001', name='衍生测试')

    def test_pct_5d(self):
        _make_bars(self.stock, [10.0, 10.0, 10.0, 10.0, 10.0, 11.0])
        rows = run_screener({'logic': 'all', 'conditions': [
            {'field': 'pct_5d', 'op': 'gte', 'value': 9},
        ]})
        self.assertEqual([r['code'] for r in rows], ['600001'])

    def test_above_ma20_needs_full_window(self):
        # 20 根：19 根 10 元 + 最新 11 元 → MA20=10.05，收盘 11 ≥ MA20
        _make_bars(self.stock, [10.0] * 19 + [11.0])
        rows = run_screener({'logic': 'all', 'conditions': [
            {'field': 'above_ma20', 'op': 'eq', 'value': 1},
        ]})
        self.assertEqual([r['code'] for r in rows], ['600001'])

    def test_insufficient_history_is_unknown_not_zero(self):
        # 只有 3 根日线：above_ma20 无值（None），eq 0 也不得命中（未知≠不成立）
        _make_bars(self.stock, [10.0, 10.0, 11.0])
        rows = run_screener({'logic': 'all', 'conditions': [
            {'field': 'above_ma20', 'op': 'eq', 'value': 0},
        ]})
        self.assertEqual(rows, [])

    def test_volume_ratio(self):
        _make_bars(self.stock, [10.0] * 5 + [11.0],
                   volumes=[100, 100, 100, 100, 100, 300])
        rows = run_screener({'logic': 'all', 'conditions': [
            {'field': 'volume_ratio', 'op': 'gt', 'value': 2},
        ]})
        self.assertEqual([r['code'] for r in rows], ['600001'])

    def test_ma5_gt_ma20(self):
        closes = [10.0] * 15 + [10.0, 10.5, 11.0, 11.5, 12.0]
        _make_bars(self.stock, closes)
        rows = run_screener({'logic': 'all', 'conditions': [
            {'field': 'ma5_gt_ma20', 'op': 'eq', 'value': 1},
        ]})
        self.assertEqual([r['code'] for r in rows], ['600001'])

    def test_new_high_and_up_days(self):
        # 单调上行 5 根：创5日(20日窗口不足 → 用 new_high_20d 需 20 根，此处单独测 up_days)
        _make_bars(self.stock, [10.0, 10.5, 11.0, 11.5, 12.0])
        rows = run_screener({'logic': 'all', 'conditions': [
            {'field': 'up_days', 'op': 'eq', 'value': 4},
        ]})
        self.assertEqual([r['code'] for r in rows], ['600001'])

    def test_new_high_20d_false_when_below_peak(self):
        closes = [10.0 + i * 0.1 for i in range(20)]  # 递增到 11.9
        closes[-1] = 11.0  # 最新收盘低于前高
        _make_bars(self.stock, closes)
        rows = run_screener({'logic': 'all', 'conditions': [
            {'field': 'new_high_20d', 'op': 'eq', 'value': 1},
        ]})
        self.assertEqual(rows, [])

    def test_bool_field_rejects_non_binary_value(self):
        _make_bars(self.stock, [10.0] * 21)
        with self.assertRaises(ConditionError):
            run_screener({'logic': 'all', 'conditions': [
                {'field': 'above_ma20', 'op': 'gt', 'value': 0.5},
            ]})


@patch('stocks.ai.valuation.fetch_valuation_map', new=_no_valuation)
class ScreenerTechIndicatorTests(TestCase):
    """技术指标字段：MACD/KDJ/RSI/BOLL 的口径与边界。"""

    def setUp(self):
        self.stock = Stock.objects.create(code='600010', name='指标测试')

    def _bars(self, closes):
        _make_bars(self.stock, closes)

    def test_macd_bullish_uptrend(self):
        # 单调上行序列：DIF>0、DIF>DEA、金叉发生在早期（此处只验多头态）
        _make_bars(self.stock, [10.0 + i * 0.1 for i in range(40)])
        rows = run_screener({'logic': 'all', 'conditions': [
            {'field': 'macd_dif_gt_zero', 'op': 'eq', 'value': 1},
            {'field': 'macd_dif_gt_dea', 'op': 'eq', 'value': 1},
        ]})
        self.assertEqual([r['code'] for r in rows], ['600010'])

    def test_macd_insufficient_history_is_unknown(self):
        _make_bars(self.stock, [10.0] * 10)
        rows = run_screener({'logic': 'all', 'conditions': [
            {'field': 'macd_dif_gt_dea', 'op': 'eq', 'value': 0},
        ]})
        self.assertEqual(rows, [])  # 未知≠0

    def test_kdj_oversold_j_low(self):
        # 连续下跌后小幅反弹：J 应处于低位
        closes = [20.0 - i * 0.2 for i in range(15)] + [17.05]
        _make_bars(self.stock, closes)
        rows = run_screener({'logic': 'all', 'conditions': [
            {'field': 'kdj_j', 'op': 'lt', 'value': 30},
        ]})
        self.assertEqual([r['code'] for r in rows], ['600010'])

    def test_rsi_extremes(self):
        # 单调上行 → RSI 应接近 100
        _make_bars(self.stock, [10.0 + i * 0.1 for i in range(20)])
        rows = run_screener({'logic': 'all', 'conditions': [
            {'field': 'rsi_14', 'op': 'gt', 'value': 80},
        ]})
        self.assertEqual([r['code'] for r in rows], ['600010'])

    def test_boll_lower_band_on_drop(self):
        # 19 根平盘 + 最后一根大跌 → 收盘应触及下轨
        _make_bars(self.stock, [10.0] * 19 + [9.0])
        rows = run_screener({'logic': 'all', 'conditions': [
            {'field': 'below_boll_lower', 'op': 'eq', 'value': 1},
        ]})
        self.assertEqual([r['code'] for r in rows], ['600010'])

    def test_new_high_60d(self):
        _make_bars(self.stock, [10.0 + i * 0.05 for i in range(61)])
        rows = run_screener({'logic': 'all', 'conditions': [
            {'field': 'new_high_60d', 'op': 'eq', 'value': 1},
        ]})
        self.assertEqual([r['code'] for r in rows], ['600010'])


class BuiltinStrategyTests(TestCase):
    """内置策略模板：全部 spec 必须可执行（与 run_screener 同一套校验）。"""

    def test_all_builtin_specs_are_valid(self):
        from .ai.screener import _validate
        from .ai.strategies import BUILTIN_STRATEGIES

        self.assertGreaterEqual(len(BUILTIN_STRATEGIES), 10)
        for s in BUILTIN_STRATEGIES:
            _validate(s['spec'])  # 不合法会抛 ConditionError

    def test_builtin_listed_by_api_and_unremovable_route(self):
        response = self.client.get('/api/screener/presets/')
        names = [p['name'] for p in response.data]
        self.assertIn('均线多头排列', names)
        first = response.data[0]
        self.assertTrue(first['builtin'])
        self.assertIsNone(first['id'])


class ValuationMapTests(SimpleTestCase):
    """腾讯估值快照解析（field 位已实测核对）。"""

    def setUp(self):
        import stocks.ai.valuation as valuation
        valuation._last_fail_at = 0.0
        from .market import _sources
        _sources._source_state.clear()
        from .market._cache import _cache
        _cache.clear()

    @patch('stocks.ai.valuation.requests.get')
    def test_parse_batch_response(self, mock_get):
        from .ai.valuation import fetch_valuation_map

        # 腾讯 v_sz600000="51~浦发~600000~...~换手38~PE39~...~流通市值44~总市值45~PB46..."
        fields = [''] * 50
        fields[1], fields[2] = '测试股', '600000'
        fields[38], fields[39] = '0.42', '5.31'
        fields[44], fields[45], fields[46] = '2307.34', '2307.36', '0.49'
        body = f'v_sh600000="{"~".join(fields)}";'
        mock_resp = mock_get.return_value
        mock_resp.content = body.encode('gbk')
        mock_resp.raise_for_status = lambda: None

        result = fetch_valuation_map(['600000'])
        row = result['600000']
        self.assertEqual(row['turnover_rate'], 0.42)
        self.assertEqual(row['pe_ttm'], 5.31)
        self.assertEqual(row['pb'], 0.49)
        self.assertAlmostEqual(row['total_mv'], 2307.36e8)  # 亿 → 元
        self.assertAlmostEqual(row['float_mv'], 2307.34e8)

    @patch('stocks.ai.valuation.requests.get')
    def test_upstream_failure_returns_empty_map(self, mock_get):
        from .ai.valuation import fetch_valuation_map

        mock_get.side_effect = RuntimeError('timeout')
        result = fetch_valuation_map(['600000'])
        self.assertEqual(result, {})  # 字段将按 None 处理，不伪造
