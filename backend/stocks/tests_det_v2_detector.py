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


def _mctx(code, buys, balance, n_bars=30, amount=1e8, as_of_idx=None):
    """合成个股 ctx：n_bars 根日线；融资明细覆盖最后 len(buys) 个 T 之前的交易日。"""
    dates = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(n_bars)]
    margin_dates = dates[-(len(buys) + 1):-1]
    rows = [{'date': d, 'rz': balance, 'rz_buy': b} for d, b in zip(margin_dates, buys)]
    return {'kind': 'stock', 'code': code, 'dates': dates, 'as_of': dates[-1],
            'amount': [amount] * n_bars, 'stock_margin': rows,
            'open': [10.0] * n_bars, 'close': [10.0] * n_bars}


class MarginCrowdingFeatureTests(TestCase):
    def test_mb5_uses_t_minus_5_to_t_minus_1(self):
        from .detector import xsection

        ctx = _mctx('600000', [1, 2, 3, 4, 5, 6], balance=100)
        value, _ = xsection.margin_buy_ratio(ctx, 5)
        self.assertAlmostEqual(value, (2 + 3 + 4 + 5 + 6) / 100)

    def test_mb5_unavailable_when_t_minus_1_missing(self):
        from .detector import xsection

        ctx = _mctx('600000', [1, 2, 3, 4, 5, 6], balance=100)
        ctx['stock_margin'] = ctx['stock_margin'][:-1]  # T−1 尚未披露
        value, reason = xsection.margin_buy_ratio(ctx, 5)
        self.assertIsNone(value)
        self.assertIn('不齐', reason)

    def test_mb5_ignores_rows_on_or_after_t(self):
        from .detector import xsection

        ctx = _mctx('600000', [1, 1, 1, 1, 1], balance=100)
        # T 日（as_of）的明细被错误塞进来：窗口对不上 T−5..T−1，宁可不可用
        ctx['stock_margin'].append({'date': ctx['as_of'], 'rz': 100, 'rz_buy': 999})
        self.assertIsNone(xsection.margin_buy_ratio(ctx, 5)[0])

    def test_non_margin_or_zero_balance_unavailable(self):
        from .detector import xsection

        self.assertIsNone(xsection.margin_buy_ratio(_mctx('600000', [], balance=0), 5)[0])
        self.assertEqual(xsection.margin_buy_ratio(_mctx('600000', [1] * 5, balance=0), 5),
                         (None, '融资余额 ≤ 0'))

    def test_dtc_uses_t_minus_20_to_t_minus_1_amounts(self):
        from .detector import xsection

        ctx = _mctx('600000', [1] * 5, balance=1e9, amount=1e8)
        ctx['amount'][-1] = 1e12  # T 日成交额不进入 DTC
        value, _ = xsection.days_to_cover(ctx, 20, 15)
        self.assertAlmostEqual(value, 10.0)

    def test_pct_rank_average_ties(self):
        from .detector import xsection

        self.assertEqual(xsection.pct_rank({'a': 1, 'b': 2, 'c': 2, 'd': 3}),
                         {'a': 0.25, 'b': 0.625, 'c': 0.625, 'd': 1.0})


_MC_PARAMS = {'mb5_days': 5, 'dtc_days': 20, 'dtc_min_days': 15,
              'veto_pct': 0.70, 'escalate_pct': 0.70, 'min_pool': 10}


def _pool(n=10, crowded_code='c9', crowded_buy=50):
    ctxs = []
    for i in range(n):
        buy = crowded_buy if f'c{i}' == crowded_code else i + 1
        ctxs.append(_mctx(f'c{i}', [buy] * 5, balance=1000, amount=1e3 * (n - i)))
    return ctxs


class MarginCrowdingDimTests(TestCase):
    def test_pool_percentile_veto_and_escalate(self):
        from .detector import xsection

        ctxs = _pool()
        pools = xsection.attach_cross_section(ctxs, {'margin_crowding': _MC_PARAMS})
        self.assertEqual(pools['mb5'], 10)
        top = xsection.eval_margin_crowding(ctxs[9], _MC_PARAMS)
        self.assertEqual(top['annotation']['mb5_pct'], 1.0)
        self.assertEqual(top['veto']['direction'], 'up')
        self.assertIn('融资拥挤', top['veto']['reason'])
        self.assertEqual(top['escalate']['direction'], 'down')
        self.assertIsNone(top['direction'])
        low = xsection.eval_margin_crowding(ctxs[0], _MC_PARAMS)
        self.assertNotIn('veto', low)
        # DTC：成交额最低（i=9）的 DTC 最高；c0 成交额最高、DTC 最低，不升级
        self.assertNotIn('escalate', low)

    def test_dtc_alone_escalates_down_but_not_veto(self):
        from .detector import xsection

        ctxs = _pool()
        xsection.attach_cross_section(ctxs, {'margin_crowding': _MC_PARAMS})
        # c7：MB5 分位 0.8 → 否决；挑 c5（MB5 0.6）但 DTC 分位 0.6 → 不升级；c8 DTC 分位 0.9
        mid = ctxs[5]
        mid['xsec']['dtc_pct'] = 0.75
        res = xsection.eval_margin_crowding(mid, _MC_PARAMS)
        self.assertNotIn('veto', res)
        self.assertIn('DTC 分位 0.75', res['escalate']['reason'])

    def test_percentile_is_pool_relative_not_fixed(self):
        from .detector import xsection

        # 整个股票池同比例放大 MB5：分位不变，否决对象不变
        a, b = _pool(), _pool()
        for c in b:
            for r in c['stock_margin']:
                r['rz_buy'] *= 10
        xsection.attach_cross_section(a, {'margin_crowding': _MC_PARAMS})
        xsection.attach_cross_section(b, {'margin_crowding': _MC_PARAMS})
        self.assertEqual([c['xsec']['mb5_pct'] for c in a], [c['xsec']['mb5_pct'] for c in b])

    def test_small_pool_or_no_xsec_is_unavailable(self):
        from .detector import xsection

        ctxs = _pool(n=5)
        xsection.attach_cross_section(ctxs, {'margin_crowding': _MC_PARAMS})
        res = xsection.eval_margin_crowding(ctxs[4], _MC_PARAMS)
        self.assertFalse(res['available'])
        self.assertFalse(xsection.eval_margin_crowding(_mctx('x', [1] * 5, 10), _MC_PARAMS)['available'])

    def test_non_margin_stock_excluded_from_pool_and_unavailable(self):
        from .detector import xsection

        ctxs = _pool() + [_mctx('nm', [], balance=0)]
        pools = xsection.attach_cross_section(ctxs, {'margin_crowding': _MC_PARAMS})
        self.assertEqual(pools['mb5'], 10)
        res = xsection.eval_margin_crowding(ctxs[-1], _MC_PARAMS)
        self.assertFalse(res['available'])
        self.assertNotIn('veto', res)


class _AllDaysCalendar:
    """测试用交易日历：每天都是交易日（不触网）。"""

    def is_trading_day(self, day):
        return True


class MarginCrowdingSignalTests(TestCase):
    """evaluate_target 里的否决与升级：投票结果不变，只修饰已出信号。"""

    def _configs(self, direction):
        from .detector import registry

        def fixed(ctx, params):
            return {'available': True, 'direction': direction, 'value': 60.0, 'note': '', 'series': []}

        spec = registry.DIMENSION_MAP['margin_crowding']
        return [
            {'key': 'ma_relation', 'name': 'a', 'weight': 1.0, 'params': {}, 'targets': ('stock',),
             'evaluate': fixed, 'enabled': True, 'group': 'price', 'role': 'direction'},
            {'key': 'big_order_net', 'name': 'b', 'weight': 1.0, 'params': {}, 'targets': ('stock',),
             'evaluate': fixed, 'enabled': True, 'group': 'funds', 'role': 'direction'},
            {'key': 'margin_crowding', 'name': spec['name'], 'weight': 1.0, 'params': dict(_MC_PARAMS),
             'targets': ('stock',), 'evaluate': spec['evaluate'], 'enabled': True,
             'group': 'leverage', 'role': 'modifier', 'vote_enabled': False},
        ]

    def _ctx(self, pct_code):
        from .detector import xsection

        ctxs = _pool()
        xsection.attach_cross_section(ctxs, {'margin_crowding': _MC_PARAMS})
        ctx = next(c for c in ctxs if c['code'] == pct_code)
        ctx['name'] = ctx['code']
        return ctx

    def _eval(self, direction, code):
        return signals.evaluate_target(self._ctx(code), self._configs(direction),
                                       cycle_counter=lambda *a: 0, calendar=_AllDaysCalendar())

    def test_up_signal_vetoed_when_crowded(self):
        sd, dims = self._eval('up', 'c9')
        self.assertIsNone(sd)
        rec = signals.vetoed_record(dims)
        self.assertEqual((rec['direction'], rec['dim']), ('up', 'margin_crowding'))
        self.assertEqual(rec['annotations']['margin_crowding']['mb5_pct'], 1.0)

    def test_up_signal_kept_when_not_crowded(self):
        sd, _dims = self._eval('up', 'c0')
        self.assertEqual(sd['direction'], 'up')
        self.assertEqual(sd['annotations']['margin_crowding']['mb5_pct'], 0.1)

    def test_down_signal_escalated_not_extra_vote(self):
        base_sd, _base_dims = signals.evaluate_target(
            self._ctx('c0'), self._configs('down'), cycle_counter=lambda *a: 0, calendar=_AllDaysCalendar())
        sd, _dims = self._eval('down', 'c9')
        self.assertEqual(sd['direction'], 'down')
        self.assertEqual(sd['vote']['n_groups'], base_sd['vote']['n_groups'])
        self.assertEqual(sd['score'], base_sd['score'])
        order = ['low', 'medium', 'high']
        self.assertEqual(order.index(sd['severity']), min(order.index(base_sd['severity']) + 1, 2))
        self.assertTrue(sd['annotations']['margin_crowding']['escalated'])


class ScanCrossSectionTests(TestCase):
    def test_scan_attaches_xsection_only_when_dim_enabled(self):
        from .detector import scan

        ctxs = _pool()
        cfg = {'key': 'margin_crowding', 'params': dict(_MC_PARAMS), 'enabled': True}
        self.assertEqual(scan._attach_cross_section(ctxs, [cfg]), {'mb5': 10, 'dtc': 10})
        self.assertEqual(ctxs[9]['xsec']['mb5_pct'], 1.0)
        fresh = _pool()
        self.assertEqual(scan._attach_cross_section(fresh, [dict(cfg, enabled=False)]), {})
        self.assertNotIn('xsec', fresh[0])

    def test_cross_section_failure_degrades_quietly(self):
        from unittest.mock import patch

        from .detector import scan, xsection

        with patch.object(xsection, 'attach_cross_section', side_effect=ValueError('boom')):
            self.assertEqual(scan._attach_cross_section(
                _pool(), [{'key': 'margin_crowding', 'params': {}, 'enabled': True}]), {})
