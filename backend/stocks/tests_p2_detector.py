"""P2 探测器方向回归测试（ALG-04/05/09/10/13/14/15、PERF-04/08/09 等）。

约定与 tests_detector 一致：交易日历 patch 为确定值，上游接口一律 patch，测试不触网。
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from .detector import calm

_TODAY = timezone.localdate()


def _dates(n):
    return [(_TODAY - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def _price_ctx(volumes, closes, highs=None, lows=None, kind='stock'):
    n = len(closes)
    return {
        'kind': kind, 'code': '000001', 'name': '测试', 'dates': _dates(n),
        'open': list(closes), 'close': list(closes),
        'high': highs if highs is not None else [c * 1.01 for c in closes],
        'low': lows if lows is not None else [c * 0.99 for c in closes],
        'volume': volumes, 'as_of': _dates(n)[-1],
    }


class CalmBaselineTests(TestCase):
    """ALG-10：平静判定的基线不含当前样本，当前值与基线同为「窗口齐全」口径。"""

    def _calm_price_ctx(self, n=45):
        volumes = [100 + (2 if i % 2 == 0 else -2) for i in range(n - 5)]
        volumes += [99.0, 101.0, 100.0, 100.5, 100.2]
        closes = [10.0] * n
        # 振幅两两不同，便于断言「当日值不在基线里」
        highs = [10.0 * (1 + 0.01 + i * 0.0001) for i in range(n)]
        lows = [10.0 * (1 - 0.01) for _ in range(n)]
        highs[-1] = 10.0 * 1.01045  # 当日振幅偏低（2.045%，与历史值都不同），应判平静
        return _price_ctx(volumes, closes, highs, lows)

    def test_baselines_exclude_current_sample(self):
        ctx = self._calm_price_ctx()
        seen = []
        real = calm._percentile

        def spy(vals, q):
            seen.append(list(vals))
            return real(vals, q)

        with patch.object(calm, '_percentile', side_effect=spy):
            ok, meta = calm.is_calm_price(ctx)
        self.assertTrue(ok, meta)
        cv_base, amp_base = seen
        amp_now = (ctx['high'][-1] - ctx['low'][-1]) / ctx['close'][-2] * 100
        self.assertNotIn(amp_now, amp_base)
        self.assertEqual(len(amp_base), 43)  # 当日之前的全部振幅（共 44 根振幅，不足 lookback=60）
        # 当前 5 日窗口的 CV 也不在 CV 基线里：基线最后一个窗口止于前一日
        vols = ctx['volume']
        last_base = vols[-6:-1]
        m = sum(last_base) / 5
        expected_last = calm._std(last_base) / m
        self.assertAlmostEqual(cv_base[-1], expected_last)

    def test_missing_today_amplitude_is_not_calm(self):
        # 旧实现先丢 None 再取 amps[-1]，当日缺最高/最低价时拿前一天冒充当日
        ctx = self._calm_price_ctx()
        ctx['high'][-1] = None
        ok, meta = calm.is_calm_price(ctx)
        self.assertFalse(ok)
        self.assertIn('当日振幅缺失', meta['reason'])

    def test_rvol_base_window_with_gap_is_not_computed(self):
        ctx = self._calm_price_ctx()
        ctx['volume'][-3] = None  # 落在 RVOL 的前 10 日基期窗口里
        ok, meta = calm.is_calm_price(ctx)
        self.assertFalse(ok)
        self.assertIn('成交量历史不足', meta['reason'])

    def test_sector_baselines_exclude_current_window(self):
        nets = [3e8 if i % 2 == 0 else -3e8 for i in range(20)]
        pcts = [0.3 + 0.01 * i if i % 2 == 0 else -0.3 for i in range(20)]
        ctx = {'kind': 'industry', 'code': 'x', 'name': 'x', 'dates': _dates(20),
               'net': nets, 'change_pct': pcts, 'as_of': _dates(20)[-1]}
        seen = []
        real = calm._percentile

        def spy(vals, q):
            seen.append(list(vals))
            return real(vals, q)

        with patch.object(calm, '_percentile', side_effect=spy):
            calm.is_calm_sector(ctx)
        flow_base, std_base = seen
        self.assertEqual(len(flow_base), 20 - 3)   # 不含末尾 3 日窗口
        self.assertEqual(len(std_base), 20 - 5)    # 不含末尾 5 日窗口
        self.assertNotIn(calm._std(pcts[-5:]), std_base)


class SignValueBoundaryTests(TestCase):
    """ALG-15：_sign_value 的零值与非法参数边界。"""

    def test_zero_raw_has_no_direction_even_when_min_is_zero(self):
        from .detector.dimensions import _sign_value

        value, direction, _note = _sign_value(0.0, {'mild': 1.0, 'min': 0.0, 'max': 2.0})
        self.assertEqual(value, 0.0)
        self.assertIsNone(direction)

    def test_invalid_params_do_not_vote(self):
        from .detector.dimensions import _sign_value

        for params in (
            {'mild': 0, 'min': 0.1, 'max': 2.0},
            {'mild': -1, 'min': 0.1, 'max': 2.0},
            {'mild': 1.0, 'min': 3.0, 'max': 2.0},
            {'mild': 1.0, 'min': -0.1, 'max': 2.0},
            {'mild': 1.0, 'min': None, 'max': 2.0},
        ):
            value, direction, note = _sign_value(0.5, params)
            self.assertIsNone(direction, params)
            self.assertIsNone(value, params)
            self.assertIn('参数非法', note)


def _random_walk_ctx(rng, n=70, sigma=0.015):
    """随机游走个股序列：日收益 N(0, sigma)，振幅 1%~3%，成交量对数正态 σ=0.3。"""
    import math

    p, closes, highs, lows, vols = 10.0, [], [], [], []
    for _ in range(n):
        pc = p
        p *= 1 + rng.gauss(0, sigma)
        closes.append(p)
        amp = abs(rng.gauss(0, 0.012)) + 0.01
        highs.append(max(p, pc) * (1 + amp / 2))
        lows.append(min(p, pc) * (1 - amp / 2))
        vols.append(1e6 * math.exp(rng.gauss(0, 0.3)))
    ctx = _price_ctx(vols, closes, highs, lows)
    ctx['dates'] = [str(i) for i in range(n)]
    ctx['as_of'] = ctx['dates'][-1]
    return ctx


def _random_sector_ctx(rng, n=45):
    dates = [str(i) for i in range(n)]
    return {'kind': 'industry', 'code': 'x', 'name': 'x', 'dates': dates,
            'net': [rng.gauss(0, 3e8) for _ in range(n)],
            'change_pct': [rng.gauss(0, 1.2) for _ in range(n)], 'as_of': dates[-1]}


class RandomWalkNoiseTests(TestCase):
    """ALG-04：纯噪声（随机游走）下，默认参数的信号率必须 < 5%。

    修复前用同样的模拟：平静个股约 31%、平静板块约 37% 出「≥2 维同向」信号。
    固定随机种子，结果确定，不会抖动。
    """

    def _rates(self, kind, gen, n, overrides=None):
        import random

        from .detector import registry

        rng = random.Random(7)
        specs = [s for s in registry.DIMENSIONS if s['enabled'] and kind in s['targets']]
        fired = fired_calm = calm_n = 0
        for _ in range(n):
            ctx = gen(rng)
            up = down = 0
            for spec in specs:
                params = {**spec['defaults'], **(overrides or {})}
                res = spec['evaluate'](ctx, params)
                up += res['direction'] == 'up'
                down += res['direction'] == 'down'
            fire = (up >= 2 and down == 0) or (down >= 2 and up == 0)
            fired += fire
            ok, _meta = calm.is_calm(ctx)
            if ok:
                calm_n += 1
                fired_calm += fire
        return fired / n, (fired_calm / calm_n if calm_n else 0.0), calm_n

    def test_stock_noise_signal_rate_below_5pct_across_volatility(self):
        for sigma in (0.008, 0.015, 0.025):
            overall, given_calm, calm_n = self._rates(
                'stock', lambda r, s=sigma: _random_walk_ctx(r, sigma=s), 800)
            self.assertGreater(calm_n, 50, sigma)
            self.assertLess(overall, 0.05, sigma)
            self.assertLess(given_calm, 0.05, sigma)

    def test_sector_noise_signal_rate_below_5pct(self):
        overall, given_calm, calm_n = self._rates('industry', _random_sector_ctx, 1500)
        self.assertGreater(calm_n, 100)
        self.assertLess(overall, 0.05)
        self.assertLess(given_calm, 0.05)

    def test_without_noise_gate_the_simulation_does_catch_noise(self):
        # 守护测试本身的灵敏度：关掉噪声闸门（旧口径）时模拟必须能测出大量噪声信号
        overall, _given_calm, _n = self._rates(
            'stock', _random_walk_ctx, 300, overrides={'noise_pct': 0})
        self.assertGreater(overall, 0.2)


class VolCompressTests(TestCase):
    """ALG-04/DET-18：压缩按自身历史分位判定，宽度用涨跌幅而不是振幅。"""

    def _ctx(self, pcts):
        n = len(pcts)
        closes, p = [], 10.0
        for x in pcts:
            p *= 1 + x / 100
            closes.append(p)
        return _price_ctx([1e6] * n, closes)

    def test_single_point_narrowing_is_not_compression(self):
        from .detector.dimensions import eval_vol_compress
        from .detector.registry import DIMENSION_MAP

        # 历史窗口宽度都很低，末段只比 3 天前略窄：旧口径（widths[-1] < widths[-4]）判压缩
        base = [0.05, -0.05] * 20
        tail = [1.0, -1.0, 1.2, -0.8, 1.1, 0.9, -0.9, 1.0, -1.0, 1.0]
        res = eval_vol_compress(self._ctx(base + tail), dict(DIMENSION_MAP['vol_compress']['defaults']))
        self.assertTrue(res['available'], res)
        self.assertIsNone(res['direction'])
        self.assertIn('未压缩', res['note'])

    def test_amplitude_does_not_drive_compression(self):
        from .detector.dimensions import eval_vol_compress
        from .detector.registry import DIMENSION_MAP

        pcts = [0.8, -0.7, 0.9, -1.0, 0.6] * 8 + [0.3, 0.35, 0.32, 0.36, 0.34]
        ctx = self._ctx(pcts)
        params = dict(DIMENSION_MAP['vol_compress']['defaults'])
        first = eval_vol_compress(ctx, params)
        # 振幅随便改，结论不变（宽度只看涨跌幅）
        ctx['high'] = [c * 1.05 for c in ctx['close']]
        second = eval_vol_compress(ctx, params)
        self.assertEqual(first['direction'], 'up', first)
        self.assertEqual(first['direction'], second['direction'])

    def test_compressed_but_insignificant_drift_does_not_vote(self):
        from .detector.dimensions import eval_vol_compress
        from .detector.registry import DIMENSION_MAP

        pcts = [0.8, -0.7, 0.9, -1.0, 0.6] * 8 + [0.3, -0.1, 0.25, -0.05, 0.2]
        res = eval_vol_compress(self._ctx(pcts), dict(DIMENSION_MAP['vol_compress']['defaults']))
        self.assertIsNone(res['direction'], res)
        self.assertIn('不显著', res['note'])


class NoiseGateTests(TestCase):
    """ALG-04：噪声闸门与 OLS 斜率的基本行为。"""

    def test_ols_slope(self):
        from .detector.dimensions import _ols_slope

        self.assertAlmostEqual(_ols_slope([1.0, 2.0, 3.0, 4.0]), 1.0)
        # 末端单日尖峰：OLS 只按权重计入，远小于两端点差分 (5-1)/3
        self.assertLess(_ols_slope([1.0, 1.0, 1.0, 5.0]), (5.0 - 1.0) / 3)
        self.assertIsNone(_ols_slope([1.0, None, 2.0]))

    def test_gate_blocks_raw_inside_own_noise_band(self):
        from .detector.dimensions import _gated

        params = {'mild': 1.0, 'min': 0.01, 'max': 5.0,
                  'noise_pct': 0.8, 'noise_window': 60, 'noise_min': 15}
        history = [0.5, -0.5] * 10
        _v, direction, note = _gated(0.3, history, params)
        self.assertIsNone(direction)
        self.assertIn('分位', note)
        _v, direction, _n = _gated(0.9, history, params)
        self.assertEqual(direction, 'up')

    def test_gate_requires_enough_history_and_can_be_disabled(self):
        from .detector.dimensions import _gated

        params = {'mild': 1.0, 'min': 0.01, 'max': 5.0,
                  'noise_pct': 0.8, 'noise_window': 60, 'noise_min': 15}
        _v, direction, note = _gated(0.9, [0.1] * 5, params)
        self.assertIsNone(direction)
        self.assertIn('样本不足', note)
        _v, direction, _n = _gated(0.9, [0.1] * 5, {**params, 'noise_pct': 0})
        self.assertEqual(direction, 'up')


class IndexDimensionCoverageTests(TestCase):
    """ALG-09：指数不再挂永远不可用的大单维度；融资余额只在沪深300上投票。"""

    def test_big_order_net_not_targeted_at_index(self):
        from .detector import registry

        self.assertNotIn('index', registry.DIMENSION_MAP['big_order_net']['targets'])
        self.assertNotIn('big_order_net', [d['key'] for d in registry.specs_for_target('index')])

    def _margin_ctx(self, code):
        totals = [1.0e12 * (1 + 0.0005 * i) for i in range(30)]
        totals[-1] *= 1.01  # 末端明显抬升
        return {'kind': 'index', 'code': code, 'margin': [
            {'date': d, 'total': t} for d, t in zip(_dates(30), totals)]}

    def test_margin_votes_only_on_hs300(self):
        from .detector.dimensions import eval_margin_north
        from .detector.registry import DIMENSION_MAP

        params = dict(DIMENSION_MAP['margin_north']['defaults'])
        others = [eval_margin_north(self._margin_ctx(c), params)
                  for c in ('sh000001', 'sz399001', 'sz399006', 'sh000688')]
        for res in others:
            self.assertFalse(res['available'])
            self.assertIsNone(res['direction'])
        hs300 = eval_margin_north(self._margin_ctx('sh000300'), params)
        self.assertTrue(hs300['available'], hs300)
        self.assertEqual(hs300['direction'], 'up', hs300)

    def test_build_index_context_reads_margin_only_for_hs300(self):
        from .detector import dimensions
        from .models import MarketDailySnapshot

        MarketDailySnapshot.objects.create(
            kind=MarketDailySnapshot.KIND_MARGIN, trade_date=_TODAY, payload=[{'total': 1.0}])
        items = [{'date': d, 'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0, 'volume': 1.0}
                 for d in _dates(40)]
        with patch('stocks.market.indices.fetch_index_daily_series',
                   return_value={'available': True, 'items': items}):
            ctx, _ = dimensions.build_index_context('sh000001', '上证指数')
            self.assertEqual(ctx['margin'], [])
            ctx, _ = dimensions.build_index_context('sh000300', '沪深300')
            self.assertEqual(len(ctx['margin']), 1)


class TurnoverShareChangeTests(TestCase):
    """ALG-13：窗口内送转导致量价基准断层时，换手率分位维度暂停。"""

    def _ctx(self, split_at=None, n=70):
        import random

        rng = random.Random(3)
        closes, vols, amounts = [], [], []
        for i in range(n):
            raw_price = 10.0 * (1 + rng.gauss(0, 0.01))
            raw_vol = 1e6 * (1 + rng.gauss(0, 0.1))
            if split_at is not None and i < split_at:
                # 10 转 10 之前：前复权价减半，成交量仍是旧股本口径，成交额不受影响
                closes.append(raw_price / 2)
                vols.append(raw_vol)
                amounts.append(raw_price * raw_vol)
            else:
                post_price = raw_price / 2 if split_at is not None else raw_price
                post_vol = raw_vol * 2 if split_at is not None else raw_vol
                closes.append(post_price)
                vols.append(post_vol)
                amounts.append(post_price * post_vol)
        ctx = _price_ctx(vols, closes)
        ctx['amount'] = amounts
        return ctx

    def test_split_inside_window_makes_dimension_unavailable(self):
        from .detector.dimensions import eval_turnover_pct
        from .detector.registry import DIMENSION_MAP

        res = eval_turnover_pct(self._ctx(split_at=60), dict(DIMENSION_MAP['turnover_pct']['defaults']))
        self.assertFalse(res['available'])
        self.assertIn('送转', res['note'])

    def test_no_split_keeps_dimension_available(self):
        from .detector.dimensions import eval_turnover_pct
        from .detector.registry import DIMENSION_MAP

        res = eval_turnover_pct(self._ctx(), dict(DIMENSION_MAP['turnover_pct']['defaults']))
        self.assertTrue(res['available'], res)

    def test_split_outside_window_is_ignored(self):
        from .detector.dimensions import _share_basis_break

        self.assertIsNone(_share_basis_break(self._ctx(split_at=5), 60, 1.2))
        self.assertIsNotNone(_share_basis_break(self._ctx(split_at=40), 60, 1.2))
