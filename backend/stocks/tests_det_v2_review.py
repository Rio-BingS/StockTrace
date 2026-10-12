"""复盘节点（det-v2）：20/60 日相对沪深300 超额，前 10 个交易日不判定失败。"""

from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase

from .detector import nodes


def _after(n, start=date(2026, 1, 5), open_=10.0, step=0.0):
    return [(start + timedelta(days=i), open_, open_ * (1 + step * (i + 1))) for i in range(n)]


def _bench(after):
    return {d: (100.0, 100.0) for d, _o, _c in after}


class JudgeNodesTests(TestCase):
    def test_first_10_days_observe_without_verdict(self):
        after = _after(6, step=-0.02)  # 向上信号前 6 日已跌 12%
        out = nodes.judge_nodes(after, 'up', _bench(after))
        self.assertEqual(out['elapsed'], 6)
        for node in out['nodes']:
            self.assertEqual(node['state'], 'observe')
            self.assertIsNone(node['verdict'])
        self.assertLess(out['nodes'][0]['dir_excess'], 0)  # 进度照常显示

    def test_between_10_and_20_pending(self):
        after = _after(15, step=-0.01)
        out = nodes.judge_nodes(after, 'up', _bench(after))
        self.assertEqual([n['state'] for n in out['nodes']], ['pending', 'pending'])

    def test_20_day_node_judged_60_pending(self):
        after = _after(25, step=-0.01)
        out = nodes.judge_nodes(after, 'down', _bench(after))
        n20, n60 = out['nodes']
        self.assertEqual((n20['state'], n20['days'], n20['end_date']), ('done', 20, str(after[19][0])))
        # 向下信号：标的跌 20%、基准持平 → 按方向超额为正，判通过
        self.assertAlmostEqual(n20['excess'], -20.0)
        self.assertAlmostEqual(n20['dir_excess'], 20.0)
        self.assertEqual(n20['verdict'], 'pass')
        self.assertEqual(n60['state'], 'pending')

    def test_60_day_node_fail_for_up_signal(self):
        after = _after(70, step=-0.001)
        out = nodes.judge_nodes(after, 'up', _bench(after))
        self.assertEqual([n['verdict'] for n in out['nodes']], ['fail', 'fail'])

    def test_base_is_t_plus_1_open(self):
        after = [(date(2026, 1, 5), 8.0, 10.0)] + _after(19, start=date(2026, 1, 6))
        out = nodes.judge_nodes(after, 'up', {})
        self.assertAlmostEqual(out['nodes'][0]['fwd_return'], 25.0)  # 10 / 8 − 1
        self.assertIsNone(out['nodes'][0]['excess'])                  # 无基准不判
        self.assertIsNone(out['nodes'][0]['verdict'])

    def test_sector_close_base_uses_one_more_day(self):
        after = [(date(2026, 1, 5) + timedelta(days=i), None, 100.0 + i) for i in range(21)]
        out = nodes.judge_nodes(after, 'up', {}, base_is_open=False)
        self.assertEqual(out['elapsed'], 20)
        self.assertEqual(out['nodes'][0]['state'], 'done')
        self.assertAlmostEqual(out['nodes'][0]['fwd_return'], 20.0)

    def test_no_data(self):
        out = nodes.judge_nodes([], 'up')
        self.assertEqual(out['elapsed'], 0)
        self.assertTrue(all(n['state'] == 'observe' for n in out['nodes']))


class ReviewNodesApiTests(TestCase):
    def setUp(self):
        p = patch('stocks.detector.views.expected_latest_date', return_value=date(2026, 3, 2))
        p.start()
        self.addCleanup(p.stop)

    def test_detail_and_list_carry_nodes_and_elapsed(self):
        from decimal import Decimal

        from .models import DailyQuote, DetectorRun, DetectorSignal, Stock

        stock = Stock.objects.create(code='600000', name='浦发银行')
        t = date(2026, 1, 2)
        for i in range(25):
            d = t + timedelta(days=i + 1)
            DailyQuote.objects.create(
                stock=stock, trade_date=d, open_price=Decimal('10'), close_price=Decimal('9'),
                high_price=Decimal('10'), low_price=Decimal('9'), open_close_diff=Decimal('0'),
                open_close_pct=Decimal('0'), high_low_diff=Decimal('0'), high_low_pct=Decimal('0'))
            DetectorRun.objects.create(trade_date=d)
        sig = DetectorSignal.objects.create(target_kind='stock', target_code='600000', target_name='浦发银行',
                                            trade_date=t, direction='down', cycles=2, score=50,
                                            status=DetectorSignal.STATUS_MISSED, review_hit=False)
        bench = {'available': True, 'items': [
            {'date': (t + timedelta(days=i + 1)).isoformat(), 'open': 100.0, 'close': 100.0} for i in range(25)]}
        with patch('stocks.market.indices.fetch_index_daily_series', return_value=bench), \
                patch('stocks.detector.views.build_stock_context', return_value=(None, 'x')):
            r = self.client.get(f'/api/detector/signals/{sig.id}/')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['signal']['elapsed_days'], 25)
        n20 = body['review_nodes']['nodes'][0]
        self.assertEqual((n20['state'], n20['verdict']), ('done', 'pass'))
        self.assertAlmostEqual(n20['dir_excess'], 10.0)
        rows = self.client.get('/api/detector/signals/', {'date': t.isoformat()}).json()['items']
        self.assertEqual(rows[0]['elapsed_days'], 25)
