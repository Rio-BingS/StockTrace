"""探测器信号接口：按来源过滤、来源字段、是否已在自选、成分池完整度摘要。"""

from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from .models import DetectorRun, DetectorSignal, Stock
from .tests_detector import _TODAY


def _sig(code, source, kind='stock', direction='down', score=50):
    return DetectorSignal.objects.create(
        target_kind=kind, target_code=code, target_name=code, trade_date=_TODAY,
        direction=direction, score=score, severity='high', source=source)


class SignalSourceFilterTests(TestCase):
    def setUp(self):
        p = patch('stocks.detector.views.expected_latest_date', return_value=_TODAY)
        p.start()
        self.addCleanup(p.stop)
        self.client = APIClient()
        DetectorRun.objects.create(
            trade_date=_TODAY, status='ok', pool_status='done',
            pool_coverage={'members': 800, 'expected': 780, 'fresh': 770, 'ratio': 0.987,
                           'missing_count': 10, 'missing': ['600000'] * 10, 'signals_pushed': 2},
            vetoed=[{'target_kind': 'stock', 'target_code': '600001', 'direction': 'up', 'source': 'pool'},
                    {'target_kind': 'stock', 'target_code': '000001', 'direction': 'up'}])
        Stock.objects.create(code='000001', name='平安银行', is_active=True)
        Stock.objects.create(code='600000', name='浦发银行', is_active=False)
        _sig('000001', 'watchlist')
        _sig('600000', 'pool')
        _sig('600519', 'pool', direction='up', score=90)
        _sig('sh000001', 'market', kind='index')

    def _get(self, **params):
        res = self.client.get('/api/detector/signals/', params)
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def test_filter_by_source(self):
        data = self._get(source='pool')
        self.assertEqual({r['target_code'] for r in data['items']}, {'600000', '600519'})
        self.assertTrue(all(r['source'] == 'pool' for r in data['items']))
        self.assertEqual(data['items'][0]['source_display'], '算法推送')
        # 向下排在向上之前（向上未经验证）
        self.assertEqual([r['direction'] for r in data['items']], ['down', 'up'])
        self.assertEqual(self._get(source='watchlist')['items'][0]['target_code'], '000001')
        self.assertEqual(self._get(source='market')['items'][0]['target_kind'], 'index')

    def test_invalid_source_400(self):
        res = self.client.get('/api/detector/signals/', {'source': 'all'})
        self.assertEqual(res.status_code, 400)

    def test_in_watchlist_flag_and_summary(self):
        data = self._get()
        by_code = {r['target_code']: r for r in data['items']}
        self.assertTrue(by_code['000001']['in_watchlist'])
        self.assertFalse(by_code['600000']['in_watchlist'])
        self.assertIsNone(by_code['sh000001']['in_watchlist'])
        self.assertEqual(data['summary']['by_source'], {'watchlist': 1, 'pool': 2, 'market': 1})
        pool = data['latest_run']['pool']
        self.assertEqual(pool['status'], 'done')
        self.assertEqual(pool['fresh'], 770)
        self.assertNotIn('missing', pool)  # 代码明细不随列表下发

    def test_vetoed_filtered_by_source_with_legacy_rows(self):
        self.assertEqual([v['target_code'] for v in self._get(source='pool')['vetoed']], ['600001'])
        # 旧记录没有 source：个股按自选
        self.assertEqual([v['target_code'] for v in self._get(source='watchlist')['vetoed']], ['000001'])

    @patch('stocks.views.watchlist._initial_fetch', return_value={'status': 'started'})
    def test_add_pool_stock_to_watchlist_reactivates_row(self, _fetch):
        res = self.client.post('/api/stocks/', {'code': '600000', 'name': '浦发银行'}, format='json')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(Stock.objects.get(code='600000').is_active)
        self.assertTrue({r['target_code']: r for r in self._get()['items']}['600000']['in_watchlist'])
