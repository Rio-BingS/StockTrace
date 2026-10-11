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
