"""P3 回归测试：AI 方向（AIX-07 服务商优先级/参数风格、AI 后台任务落库、选股翻译/点评异步化）。"""
from types import SimpleNamespace

from django.test import TestCase
from rest_framework.test import APIClient

from .tests_p2_sec_ai_ops import _ai_fixtures, _ai_patches


def _rows(resp):
    body = resp.json()
    return body['results'] if isinstance(body, dict) and 'results' in body else body


class AiProviderPriorityTests(TestCase):
    """AIX-07：priority 越小越优先，列表顺序与调用回退顺序一致；param_style 可配置。"""

    def setUp(self):
        from stocks.ai.crypto import encrypt_api_key
        from stocks.models import AiProvider
        self.p1, self.stock = _ai_fixtures()
        # 后录入但优先级更高（数字更小）
        self.p2 = AiProvider.objects.create(
            name='P2', base_url='https://b.com', model='m2', priority=10,
            api_key_encrypted=encrypt_api_key('sk-p2-1111'),
        )
        self.post = _ai_patches(self)
        self.client = APIClient()

    def _urls(self):
        return [c.args[0] for c in self.post.call_args_list]

    def test_defaults(self):
        self.assertEqual(self.p1.priority, 100)
        self.assertEqual(self.p1.param_style, 'auto')

    def test_selection_and_list_follow_priority(self):
        from stocks.ai.llm import enabled_providers
        from stocks.tests_ai import _fake_response
        from stocks.tests_p1_ai import _ok
        self.assertEqual([p.id for p in enabled_providers()], [self.p2.id, self.p1.id])
        listed = [p['id'] for p in _rows(self.client.get('/api/ai-providers/'))]
        self.assertEqual(listed, [self.p2.id, self.p1.id])
        # 优先级高的先调，鉴权失败回退到下一个
        self.post.side_effect = [_fake_response(401, text='no'), _ok('回退成功')]
        r = self.client.post(f'/api/stocks/{self.stock.id}/ai-analysis/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._urls(), ['https://b.com/chat/completions',
                                        'https://mock.local/chat/completions'])

    def test_same_priority_falls_back_to_creation_order(self):
        from stocks.ai.llm import enabled_providers
        self.p2.priority = 100
        self.p2.save()
        self.assertEqual([p.id for p in enabled_providers()], [self.p1.id, self.p2.id])

    def test_serializer_reads_and_writes_fields(self):
        r = self.client.patch(f'/api/ai-providers/{self.p1.id}/',
                              {'priority': 1, 'param_style': 'reasoning'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.data['priority'], r.data['param_style']), (1, 'reasoning'))
        self.p1.refresh_from_db()
        self.assertEqual((self.p1.priority, self.p1.param_style), (1, 'reasoning'))
        listed = [p['id'] for p in _rows(self.client.get('/api/ai-providers/'))]
        self.assertEqual(listed[0], self.p1.id)

    def test_create_with_fields(self):
        r = self.client.post('/api/ai-providers/', {
            'name': 'N', 'base_url': 'https://b.com', 'model': 'o3-mini', 'api_key': 'sk-new-2222',
            'priority': 5, 'param_style': 'openai_reasoning',
        }, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data['priority'], 5)
        self.assertEqual(r.data['param_style'], 'openai_reasoning')

    def test_invalid_values_rejected(self):
        url = f'/api/ai-providers/{self.p1.id}/'
        for bad in (-1, 10000):
            r = self.client.patch(url, {'priority': bad}, format='json')
            self.assertEqual(r.status_code, 400, bad)
            self.assertIn('priority', r.data)
        r = self.client.patch(url, {'param_style': 'turbo'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('param_style', r.data)

    def test_param_style_field_drives_payload(self):
        from stocks.ai.llm import build_payload
        # auto：按模型名推断
        p = build_payload(SimpleNamespace(model='o1', param_style='auto'), [], 0.3, 100)
        self.assertEqual(p.get('max_completion_tokens'), 100)
        self.assertNotIn('temperature', p)
        # 显式 reasoning：模型名看不出是推理模型，也不传 temperature
        self.p1.param_style = 'reasoning'
        p = build_payload(self.p1, [], 0.3, 100)
        self.assertNotIn('temperature', p)
        self.assertEqual(p['max_tokens'], 100)
        # 显式 standard 覆盖模型名推断
        self.p1.model, self.p1.param_style = 'deepseek-reasoner', 'standard'
        p = build_payload(self.p1, [], 0.3, 100)
        self.assertEqual((p['temperature'], p['max_tokens']), (0.3, 100))
