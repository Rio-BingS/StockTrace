"""OPS-08：AI 端点端到端测试——不再把 LlmClient 整个 mock 掉。

既有视图用例 patch('stocks.ai.views.LlmClient')，LlmClient 的请求组装、响应解析、
错误分类与流水记账都没被视图测试走到。这里只 mock HTTP 层（requests.post）与
DNS（socket.getaddrinfo），视图 → LlmClient → _extract_text/_parse_json_object 全链路真实执行。
"""

import json
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import requests
from django.test import TestCase, override_settings

from .ai.crypto import encrypt_api_key
from .models import AiCallLog, AiProvider, DailyQuote, Stock
from .tests_ai import _PUBLIC_DNS, _fake_getaddrinfo, _fake_response


def _completion(content, finish_reason='stop', usage=None):
    return _fake_response(200, json_data={
        'choices': [{'message': {'content': content}, 'finish_reason': finish_reason}],
        'usage': usage if usage is not None else {'prompt_tokens': 11, 'completion_tokens': 7},
    })


def _no_valuation(codes):
    return {}


@override_settings(STOCKTRACE_LLM_MAX_RETRIES=0)
@patch('stocks.ai.valuation.fetch_valuation_map', new=_no_valuation)
@patch('stocks.ai.llm.socket.getaddrinfo', side_effect=_fake_getaddrinfo(_PUBLIC_DNS))
@patch('stocks.ai.llm.requests.post')
class AiViewsThroughRealClientTests(TestCase):
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
        self.provider = AiProvider.objects.create(
            name='Mock', base_url='https://mock.local/v1/', model='mock-1',
            api_key_encrypted=encrypt_api_key('sk-e2e-4242'),
        )

    def _analysis(self):
        return self.client.post(f'/api/stocks/{self.stock.id}/ai-analysis/')

    def test_analysis_builds_openai_request_and_logs_usage(self, post, _dns):
        post.return_value = _completion('  趋势偏强  ')
        resp = self._analysis()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['analysis'], '趋势偏强')
        url = post.call_args.args[0]
        kw = post.call_args.kwargs
        self.assertEqual(url, 'https://mock.local/v1/chat/completions')
        self.assertEqual(kw['headers']['Authorization'], 'Bearer sk-e2e-4242')  # 解密后的 Key
        self.assertIs(kw['allow_redirects'], False)
        self.assertEqual(kw['json']['model'], 'mock-1')
        self.assertEqual(kw['json']['messages'][0]['role'], 'system')
        self.assertIn('000001', json.dumps(kw['json']['messages'], ensure_ascii=False))
        log = AiCallLog.objects.get(purpose='analysis')
        self.assertTrue(log.success)
        self.assertEqual((log.prompt_tokens, log.completion_tokens), (11, 7))

    def test_analysis_content_none_is_empty_error(self, post, _dns):
        post.return_value = _completion(None)
        resp = self._analysis()
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.data['error'], 'empty')
        self.assertFalse(AiCallLog.objects.get(purpose='analysis').success)

    def test_analysis_truncated_output_not_returned(self, post, _dns):
        post.return_value = _completion('半截内容', finish_reason='length')
        resp = self._analysis()
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.data['error'], 'truncated')
        self.assertNotIn('半截内容', json.dumps(resp.data, ensure_ascii=False))

    def test_analysis_non_200_classified_without_body(self, post, _dns):
        for code, kind in ((401, 'auth'), (429, 'rate_limit'), (503, 'upstream')):
            with self.subTest(code=code):
                AiCallLog.objects.all().delete()  # 避开同股冷却
                post.return_value = _fake_response(code, text='secret upstream body')
                resp = self._analysis()
                self.assertEqual(resp.status_code, 502)
                self.assertEqual(resp.data['error'], kind)
                self.assertNotIn('secret upstream body', json.dumps(resp.data, ensure_ascii=False))

    def test_analysis_bad_json_and_network_errors(self, post, _dns):
        post.return_value = _fake_response(200, text='<html>')
        self.assertEqual(self._analysis().data['error'], 'bad_response')
        AiCallLog.objects.all().delete()
        post.return_value = None
        post.side_effect = requests.ConnectionError('reset')
        self.assertEqual(self._analysis().data['error'], 'network')

    def test_screener_ai_parses_fenced_json_and_executes(self, post, _dns):
        spec = {'logic': 'all', 'conditions': [{'field': 'change_pct', 'op': 'gt', 'value': 3}]}
        post.return_value = _completion('```json\n' + json.dumps(spec) + '\n```')
        resp = self.client.post('/api/screener/ai/', {'query': '涨3个点以上'},
                                content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['conditions'], spec)
        self.assertEqual(resp.data['count'], 1)
        self.assertEqual(post.call_args.kwargs['json']['response_format'], {'type': 'json_object'})

    def test_screener_ai_falls_back_when_json_mode_unsupported(self, post, _dns):
        spec = {'logic': 'all', 'conditions': [{'field': 'change_pct', 'op': 'gt', 'value': 3}]}
        post.side_effect = [_fake_response(400, text='response_format unsupported'),
                            _completion('好的：' + json.dumps(spec))]
        resp = self.client.post('/api/screener/ai/', {'query': '涨3个点以上'},
                                content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(post.call_count, 2)
        self.assertNotIn('response_format', post.call_args_list[1].kwargs['json'])

    def test_screener_ai_non_json_reply_is_bad_response(self, post, _dns):
        post.return_value = _completion('我无法理解这个条件')
        resp = self.client.post('/api/screener/ai/', {'query': '随便'},
                                content_type='application/json')
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.data['error'], 'bad_response')

    def test_provider_test_treats_empty_reply_as_connected(self, post, _dns):
        post.return_value = _completion('', finish_reason='length', usage={'prompt_tokens': 5})
        resp = self.client.post(f'/api/ai-providers/{self.provider.id}/test/')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(post.call_args.kwargs['json']['max_tokens'], 10)
