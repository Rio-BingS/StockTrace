"""探测器 v2：个股融资上下文、融资拥挤度、解禁事件、日内追买、趋势状态（det-v2）。

防前视是这些测试的重点：扫描日 T 只能用 T 日及以前的行情、T−1 及以前的两融，
解禁只看 T 之后已排期的日程且解禁日当天起失效。
"""

from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase

from .detector import dimensions, signals
from .models import DailyQuote, Stock, StockMarginDaily


def _quotes(stock, start, n, close=10.0):
    DailyQuote.objects.bulk_create([
        DailyQuote(
            stock=stock, trade_date=start + timedelta(days=i), open_price=Decimal(str(close)),
            high_price=Decimal(str(close * 1.01)), low_price=Decimal(str(close * 0.99)),
            close_price=Decimal(str(close)), open_close_diff=Decimal('0'), open_close_pct=Decimal('0'),
            high_low_diff=Decimal('0.2'), high_low_pct=Decimal('2'), volume=1000000,
            turnover=Decimal('10000000'), is_final=True,
        )
        for i in range(n)
    ])


class StockMarginContextTests(TestCase):
    def test_context_has_margin_rows_strictly_before_as_of(self):
        stock = Stock.objects.create(code='600000', name='浦发银行')
        start = date(2026, 1, 1)
        _quotes(stock, start, 60)
        as_of = start + timedelta(days=59)
        for i in range(3):
            StockMarginDaily.objects.create(code='600000', trade_date=as_of - timedelta(days=2 - i),
                                            exchange='sse', rz=100 + i, rz_buy=10 + i)
        ctx, err = dimensions.build_stock_context(stock)
        self.assertEqual(err, '')
        # T 日自身的明细（若已落库）也不能进 ctx：T+1 才披露
        self.assertEqual([r['date'] for r in ctx['stock_margin']],
                         [(as_of - timedelta(days=2)).isoformat(), (as_of - timedelta(days=1)).isoformat()])
        self.assertEqual(ctx['stock_margin'][-1], {'date': (as_of - timedelta(days=1)).isoformat(),
                                                   'rz': 101, 'rz_buy': 11})

    def test_non_margin_stock_has_empty_series(self):
        stock = Stock.objects.create(code='000002', name='万科A')
        _quotes(stock, date(2026, 1, 1), 40)
        ctx, _ = dimensions.build_stock_context(stock)
        self.assertEqual(ctx['stock_margin'], [])

    def test_slice_ctx_drops_margin_rows_on_or_after_cut(self):
        dates = ['2026-01-05', '2026-01-06', '2026-01-07']
        ctx = {'dates': dates, 'close': [1, 2, 3], 'as_of': dates[-1],
               'stock_margin': [{'date': d, 'rz': 1, 'rz_buy': 1} for d in ('2026-01-05', '2026-01-06')]}
        cut = signals.slice_ctx(ctx, 1)
        self.assertEqual([r['date'] for r in cut['stock_margin']], ['2026-01-05'])
        self.assertEqual(cut['close'], [1, 2])


def _res(direction, value=60.0, weight=1.0, group=None, role='direction', votes=True):
    return {'available': True, 'direction': direction, 'value': value, 'weight': weight,
            'group': group, 'role': role, 'vote_enabled': votes}


class NonVotingDimTests(TestCase):
    """vote_enabled=False 的维度照常评估，但不计票、不算可用信息组。"""

    def test_display_only_dim_never_adds_a_vote(self):
        base = {'ma_relation': _res('down', group='price'),
                'vol_ratio_slope': _res('up', group='volume', role='strength')}
        before = signals.vote(base)
        with_display = dict(base, lift_pressure=_res('down', 80, group='event', votes=False),
                            margin_crowding=_res('down', 90, group='leverage', votes=False))
        after = signals.vote(with_display)
        self.assertEqual(after['direction'], before['direction'])
        self.assertEqual(after['support'], before['support'])
        self.assertEqual(after['groups_available'], before['groups_available'])
        self.assertNotIn('lift_pressure', after['counted'])

    def test_display_only_dim_cannot_create_a_signal(self):
        # 单个方向组 + 只展示维度：仍不够两组，不出信号
        res = signals.vote({'ma_relation': _res('down', group='price'),
                            'lift_pressure': _res('down', 100, group='event', votes=False)})
        self.assertIsNone(res['direction'])

    def test_null_fire_probability_ignores_display_dims(self):
        from .detector import scan

        configs = [
            {'key': 'ma_relation', 'targets': ('stock',), 'evaluate': lambda c, p: None, 'weight': 1.0,
             'params': {}, 'enabled': True},
            {'key': 'vol_compress', 'targets': ('stock',), 'evaluate': lambda c, p: None, 'weight': 1.0,
             'params': {}, 'enabled': True},
        ]
        base = scan.null_fire_probability(configs, 'stock')
        extra = configs + [{'key': 'x_display', 'targets': ('stock',), 'evaluate': lambda c, p: None,
                            'weight': 1.0, 'params': {}, 'enabled': True, 'vote_enabled': False}]
        self.assertEqual(scan.null_fire_probability(extra, 'stock'), base)


def _signal_data(direction='up', severity='low'):
    return {'target_kind': 'stock', 'target_code': '600000', 'target_name': '浦发银行',
            'trade_date': '2026-10-09', 'direction': direction, 'cycles': 2, 'score': 50.0,
            'severity': severity}


class ModifierTests(TestCase):
    """修饰维度：否决只作用于同向已出信号；升级只升一档、不加票；附注照写。"""

    def test_veto_matching_direction(self):
        sd = _signal_data('up')
        dims = {'margin_crowding': {'available': True, 'veto': {'direction': 'up', 'reason': '融资拥挤'},
                                    'annotation': {'mb5_pct': 0.9}}}
        rec = signals.apply_modifiers(sd, dims)
        self.assertEqual((rec['dim'], rec['reason'], rec['direction']), ('margin_crowding', '融资拥挤', 'up'))
        self.assertTrue(sd['annotations']['margin_crowding']['vetoed'])

    def test_veto_other_direction_only_annotates(self):
        sd = _signal_data('down')
        dims = {'margin_crowding': {'available': True, 'veto': {'direction': 'up', 'reason': 'x'},
                                    'annotation': {'mb5_pct': 0.9}}}
        self.assertIsNone(signals.apply_modifiers(sd, dims))
        self.assertEqual(sd['annotations'], {'margin_crowding': {'mb5_pct': 0.9}})
        self.assertEqual(sd['severity'], 'low')

    def test_escalate_one_level_and_caps_at_high(self):
        for before, after in (('low', 'medium'), ('medium', 'high'), ('high', 'high')):
            sd = _signal_data('down', before)
            signals.apply_modifiers(sd, {'margin_crowding': {
                'available': True, 'escalate': {'direction': 'down', 'reason': 'MB5 高'}}})
            self.assertEqual(sd['severity'], after)
            self.assertEqual(sd['annotations']['margin_crowding']['severity_before'], before)

    def test_unavailable_modifier_ignored(self):
        sd = _signal_data('up')
        self.assertIsNone(signals.apply_modifiers(sd, {'m': {'available': False,
                                                            'veto': {'direction': 'up'}}}))
        self.assertEqual(sd['annotations'], {})


class VetoedRunRecordTests(TestCase):
    def setUp(self):
        from unittest.mock import patch

        p = patch('stocks.detector.views.expected_latest_date', return_value=date(2026, 10, 9))
        p.start()
        self.addCleanup(p.stop)

    def test_vetoed_signals_listed_with_filters(self):
        from .models import DetectorRun

        DetectorRun.objects.create(trade_date=date(2026, 10, 9), vetoed=[
            {'target_kind': 'stock', 'target_code': '600000', 'direction': 'up', 'reason': '融资拥挤'},
            {'target_kind': 'index', 'target_code': 'sh000300', 'direction': 'up', 'reason': 'x'},
        ])
        r = self.client.get('/api/detector/signals/', {'target_kind': 'stock'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([v['target_code'] for v in r.json()['vetoed']], ['600000'])
        r = self.client.get('/api/detector/signals/', {'direction': 'down'})
        self.assertEqual(r.json()['vetoed'], [])
        # 历史视图不附带否决记录
        self.assertEqual(self.client.get('/api/detector/signals/', {'all_dates': '1'}).json()['vetoed'], [])
