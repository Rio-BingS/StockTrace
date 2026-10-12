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
