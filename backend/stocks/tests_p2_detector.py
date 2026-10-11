"""P2 探测器方向回归测试（ALG-04/05/09/10/13/14/15、PERF-04/08/09 等）。

约定与 tests_detector 一致：交易日历 patch 为确定值，上游接口一律 patch，测试不触网。
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
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

    def test_baselines_exclude_current_window(self):
        # regime 化（#2）后基线仍不含当前窗口（ALG-10）：下标 e 的基线止于 e−1
        metric = [float(i) for i in range(30)]
        self.assertEqual(calm._base(metric, 29, 60), metric[:29])
        self.assertEqual(calm._base(metric, 29, 10), metric[19:29])
        self.assertNotIn(metric[29], calm._base(metric, 29, 60))

    def test_regime_uses_lagged_window_only(self):
        # 此前平静只看止于 T−calm_lag 的窗口：近 3 日任意改动不改变 regime 判定
        ctx = self._calm_price_ctx()
        ctx['high'] = [10.1] * len(ctx['close'])  # 振幅恒定，回到「此前平静」
        ok, meta = calm.is_calm_price(ctx)
        self.assertTrue(ok, meta)
        ctx['volume'][-2] = 160.0  # T−1 温和放量：不进滞后窗口，RVOL 也在区间内
        ok2, meta2 = calm.is_calm_price(ctx)
        self.assertTrue(ok2, meta2)

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

    def test_sector_recent_flow_rise_is_calm_but_spike_is_not(self):
        # 近 3 日资金温和流入留给 big_order_net 去测（#2）；突增 10 倍才排除
        mags = [2.0, 4.5, 3.0, 5.0, 2.5, 4.0, 3.5, 2.2, 4.8, 3.2]
        nets = [(mags[i % 10] if i % 2 == 0 else -mags[i % 10]) * 1e8 for i in range(21)] + [1e8] * 3
        pcts = [0.3 + 0.02 * (i % 5) if i % 2 == 0 else -0.3 for i in range(27)]
        ctx = {'kind': 'industry', 'code': 'x', 'name': 'x', 'dates': _dates(27),
               'net': nets + [3e8, 3.5e8, 4e8], 'change_pct': pcts, 'as_of': _dates(27)[-1]}
        ok, meta = calm.is_calm_sector(ctx)
        self.assertTrue(ok, meta)
        ctx['net'] = nets + [30e8, 32e8, 31e8]
        ok, meta = calm.is_calm_sector(ctx)
        self.assertFalse(ok)
        self.assertIn('净额', meta['reason'])


class CalmRegimeTests(TestCase):
    """#2 平静闸门 regime 化：迟滞、TR 振幅、温和放量、价格已大动。"""

    def _calm_ctx(self, n=60):
        # 量能起伏与振幅都缓慢收窄：滞后窗口必然处在自身历史低位（此前平静）
        volumes = [100 + (3 - i * 0.03) * (1 if i % 2 == 0 else -1) for i in range(n)]
        closes = [10.0 * (1 + (0.004 if i % 2 == 0 else -0.004)) for i in range(n)]
        highs = [c * (1.012 - i * 0.00005) for i, c in enumerate(closes)]
        lows = [c * (0.988 + i * 0.00005) for i, c in enumerate(closes)]
        return _price_ctx(volumes, closes, highs, lows)

    def test_hysteresis_keeps_calm_inside_exit_band(self):
        params = {'calm_lag': 0, 'lookback': 60, 'exit_pct': 0.6, 'hysteresis_days': 10,
                  'regime_min_samples': 20}
        base = [float(i % 10) for i in range(56)]
        # 已平静（2 ≤ 40 分位）后出现 5（40~60 分位之间）：维持
        state, streak, _r = calm._regime([('量能波动', base + [2, 2, 2, 5], 0.4)], 60, params)
        self.assertTrue(state)
        self.assertGreaterEqual(streak, 4)
        # 从不平静（7）直接到 5：进入要 ≤ 40 分位，不平静
        state, _s, reason = calm._regime([('量能波动', base + [7, 7, 7, 5], 0.4)], 60, params)
        self.assertFalse(state)
        self.assertIn('量能波动', reason)

    def test_gap_counts_in_true_range(self):
        # 跳空 2.9% 但日内窄幅 0.4%：旧 (H−L)/前收 算振幅低位；TR≈3.1% 高于历史
        # 约 2.4% 的 90 分位，按剧烈波动排除（3 日累计仍未到「已大动」）
        ctx = self._calm_ctx()
        c = ctx['close'][-2] * 1.029
        ctx['close'][-1] = ctx['open'][-1] = c
        ctx['high'][-1], ctx['low'][-1] = c * 1.002, c * 0.998
        ok, meta = calm.is_calm_price(ctx)
        self.assertFalse(ok)
        self.assertIn('真实波幅', meta['reason'])

    def test_mild_volume_ramp_is_still_calm(self):
        # 量先于价：近 3 日量能温和抬升（旧闸门 RVOL≤1.5 / 当日 CV 低位会拦下）
        ctx = self._calm_ctx()
        ctx['volume'][-3:] = [140.0, 170.0, 200.0]
        ok, meta = calm.is_calm_price(ctx)
        self.assertTrue(ok, meta)
        self.assertGreater(meta['rvol'], 1.5)

    def test_event_volume_spike_is_not_calm(self):
        ctx = self._calm_ctx()
        ctx['volume'][-1] = 400.0
        ok, meta = calm.is_calm_price(ctx)
        self.assertFalse(ok)
        self.assertIn('RVOL', meta['reason'])

    def test_price_already_moved_is_not_calm(self):
        ctx = self._calm_ctx()
        for i in (-3, -2, -1):
            ctx['close'][i] = ctx['close'][i - 1] * 1.015  # σ≈0.8%，3 日 +4.6% > 2.5σ√3≈3.5%
            ctx['high'][i], ctx['low'][i] = ctx['close'][i] * 1.01, ctx['close'][i] * 0.99
        ok, meta = calm.is_calm_price(ctx)
        self.assertFalse(ok)
        self.assertIn('已大动', meta['reason'])

    def test_adjusted_volume_is_preferred(self):
        # #15 接口：送转校正量优先于原始量（原始量在送转日翻倍会被当成放量）
        ctx = self._calm_ctx()
        ctx['volume_adj'] = list(ctx['volume'])
        ctx['volume'] = ctx['volume'][:-1] + [500.0]
        ok, meta = calm.is_calm_price(ctx)
        self.assertTrue(ok, meta)


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
        # 守护测试本身的灵敏度：关掉噪声闸门（旧口径）时模拟必须能测出大量噪声信号。
        # 价格类维度的闸门是解析 z 下限（#1/#3），z_lo=0 即关闭
        overall, _given_calm, _n = self._rates(
            'stock', _random_walk_ctx, 300, overrides={'noise_pct': 0, 'z_lo': 0.0})
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

        # 尾段压缩且漂移相对长窗口 σ 显著（z≈1.9，#3）
        pcts = [0.8, -0.7, 0.9, -1.0, 0.6] * 8 + [0.7, 0.75, 0.72, 0.76, 0.74]
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
        totals[-1] *= 1.003  # 末端抬升（仍在自身噪声带 noise_hi 倍以内，#1）
        # #9：维度读融资余额 rz（不是融资融券余额 total）
        return {'kind': 'index', 'code': code, 'margin': [
            {'date': d, 'rz': t, 'total': t * 1.01} for d, t in zip(_dates(30), totals)]}

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

        # 两融 T+1：指数数据日 T 只配 T−1 的融资余额（#9），T 当天的行不读
        for day, rz in ((_TODAY - timedelta(days=1), 0.99), (_TODAY, 0.98)):
            MarketDailySnapshot.objects.create(
                kind=MarketDailySnapshot.KIND_MARGIN, trade_date=day,
                payload=[{'total': 1.0, 'rz': rz, 'sz_total': 0.5}])
        items = [{'date': d, 'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0, 'volume': 1.0}
                 for d in _dates(40)]
        with patch('stocks.market.indices.fetch_index_daily_series',
                   return_value={'available': True, 'items': items}), \
                patch('stocks.trading_calendar.is_trading_day', lambda day=None: True):
            ctx, _ = dimensions.build_index_context('sh000001', '上证指数')
            self.assertEqual(ctx['margin'], [])
            ctx, _ = dimensions.build_index_context('sh000300', '沪深300')
            self.assertEqual([m['rz'] for m in ctx['margin']], [0.99])
            self.assertEqual(ctx['margin_as_of'], (_TODAY - timedelta(days=1)).isoformat())


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


@patch('stocks.trading_calendar.is_trading_day', lambda day=None: True)
class SectorSnapshotPreloadTests(TestCase):
    """PERF-04：一次扫描每类板块快照只读一次，各板块共用，结果与逐板块读库一致。"""

    def _seed(self, names, days=20):
        from .models import MarketDailySnapshot

        for i in range(days):
            MarketDailySnapshot.objects.create(
                kind=MarketDailySnapshot.KIND_INDUSTRY_FF,
                trade_date=_TODAY - timedelta(days=days - 1 - i),
                payload=[{'name': nm, 'net': 1e8 * (i % 3 - 1), 'change_pct': 0.1 * (i % 5)}
                         for nm in names],
            )

    def test_preloaded_context_matches_direct_read(self):
        from .detector import dimensions

        self._seed(['甲', '乙'])
        snaps = dimensions.load_sector_snapshots('industry')
        for name in ('甲', '乙'):
            direct, _ = dimensions.build_sector_context('industry', name)
            shared, _ = dimensions.build_sector_context('industry', name, snapshots=snaps)
            self.assertEqual(direct, shared)

    def test_collect_and_build_read_each_kind_once(self):
        from .detector import dimensions, scan

        self._seed(['甲', '乙', '丙'])
        real = dimensions.load_sector_snapshots
        with patch.object(scan, 'load_sector_snapshots', side_effect=real) as spy:
            targets = [t for t in scan._collect_targets() if t['kind'] == 'industry']
            ctxs = [scan._build_context(t)[0] for t in targets]
        self.assertEqual(spy.call_count, 2)  # industry + concept 各一次
        self.assertEqual([c['code'] for c in ctxs], ['丙', '乙', '甲'])
        with self.assertNumQueries(0):
            for t in targets:
                scan._build_context(t)


class CycleCountTests(TestCase):
    """ALG-15/PERF-08：周期计数一次查询；日历不可用时如实「未知」，不按自然日推进。"""

    def _sig(self, day, direction='up'):
        from .models import DetectorSignal

        return DetectorSignal.objects.create(
            target_kind='stock', target_code='600000', target_name='x',
            trade_date=day, direction=direction)

    def test_counts_consecutive_trading_days_across_weekend_in_one_query(self):
        from .detector import signals

        # 周一信号日，上周五、周四有信号：周末不是交易日，连续性不断
        monday = _TODAY - timedelta(days=_TODAY.weekday()) + timedelta(days=7)
        self._sig(monday - timedelta(days=3))
        self._sig(monday - timedelta(days=4))
        self._sig(monday - timedelta(days=6), direction='down')  # 上周二反向，断链

        def weekday_only(day=None):
            return day.weekday() < 5

        with patch('stocks.trading_calendar.is_trading_day', weekday_only), self.assertNumQueries(1):
            cycles = signals._count_cycles('stock', '600000', 'up', monday)
        self.assertEqual(cycles, 2)

    def test_unknown_calendar_returns_none_instead_of_natural_days(self):
        from .detector import signals

        self._sig(_TODAY - timedelta(days=1))
        with patch('stocks.trading_calendar.is_trading_day', return_value=None):
            self.assertIsNone(signals._count_cycles('stock', '600000', 'up', _TODAY))
        with patch('stocks.trading_calendar.is_trading_day', side_effect=RuntimeError('down')):
            self.assertIsNone(signals._count_cycles('stock', '600000', 'up', _TODAY))

    def test_unknown_cycles_marks_signal_and_counts_as_one(self):
        from .detector import signals

        def ev(ctx, params):
            return {'available': True, 'value': 30.0, 'direction': 'up', 'note': '', 'series': []}

        configs = [{'key': k, 'name': k, 'weight': 1.0, 'params': {}, 'targets': ('stock',),
                    'evaluate': ev, 'enabled': True, 'defaults': {}} for k in ('a', 'b')]
        ctx = {'kind': 'stock', 'code': '600000', 'name': 'x', 'as_of': _TODAY.isoformat()}
        with patch('stocks.trading_calendar.is_trading_day', return_value=None):
            data, _ = signals.evaluate_target(ctx, configs)
        self.assertEqual(data['cycles'], 1)
        self.assertTrue(data['cycles_unknown'])


def _always_trading(day=None):
    return True


@patch('stocks.detector.review._benchmark_bars', lambda cache: {})
@patch('stocks.trading_calendar.is_trading_day', _always_trading)
class ReviewProtocolTests(TestCase):
    """ALG-05：回看要求满 5 个交易日、基期 T+1 开盘、MAE 先到判未兑现、基线比较、段首计样本。"""

    def setUp(self):
        from .tests_detector import ReviewTests

        self.seed = ReviewTests()

    def _review_outcome(self, code, base_date, direction='up', cycles=2):
        from .detector import review

        sig = self.seed._make_signal(code, base_date, direction=direction, cycles=cycles)
        return review._stock_outcome(sig, {})

    def test_incomplete_window_stays_pending(self):
        _s, base_date = self.seed._make_stock_with_moves('600101', [2.0, 2.5, 3.0])
        self.assertIsNone(self._review_outcome('600101', base_date))

    def test_gap_inside_window_stays_pending(self):
        from .models import DailyQuote

        _s, base_date = self.seed._make_stock_with_moves('600102', [1.0, 2.0, 2.5, 3.0, 3.5, 3.6])
        DailyQuote.objects.filter(stock__code='600102', trade_date=base_date + timedelta(days=2)).delete()
        self.assertIsNone(self._review_outcome('600102', base_date))

    def test_adverse_excursion_first_is_a_miss(self):
        # 先跌 3% 再涨回 +3%：旧口径（只看 MFE）算兑现，新口径 MAE 先触及阈值 → 未兑现
        _s, base_date = self.seed._make_stock_with_moves('600103', [0.0, -3.0, -1.0, 2.0, 3.0])
        out = self._review_outcome('600103', base_date)
        self.assertIsNotNone(out)
        self.assertFalse(out['hit'])
        self.assertLess(out['thr'], 3.0)  # 窗口末 +3% 早已超过阈值，但 MAE 先到

    def test_base_is_next_day_open(self):
        from decimal import Decimal

        from .models import DailyQuote

        # T+1 跳空高开 3%，之后窗口内基本横盘：相对 T 日收盘涨了 3%，但 T 日收盘买不到
        _s, base_date = self.seed._make_stock_with_moves('600104', [3.0, 3.0, 3.05, 3.0, 2.95])
        DailyQuote.objects.filter(stock__code='600104', trade_date=base_date + timedelta(days=1)) \
            .update(open_price=Decimal('10.300'))
        out = self._review_outcome('600104', base_date)
        self.assertIsNotNone(out)
        self.assertFalse(out['hit'])

    def test_threshold_scales_with_own_volatility(self):
        from .detector import review

        _s, base_date = self.seed._make_stock_with_moves('600105', [0.5, 1.0, 1.5, 2.0, 2.5])
        out = self._review_outcome('600105', base_date)
        self.assertIsNotNone(out)
        self.assertLess(out['thr'], review.HIT_PCT)  # 历史 σ≈0.2，低于固定 1.5%
        self.assertIsNotNone(out['baseline'])

    @override_settings(DETECTOR_AUTO_TUNE=True)
    def test_only_segment_head_counts_for_tuning(self):
        from .detector import review, scan
        from .models import DetectorDimConfig

        for i in range(review.MIN_ADJ_SAMPLES):
            code = f'6006{i}0'
            _s, base_date = self.seed._make_stock_with_moves(
                code, [0.5, 1.2, 2.2, 2.5, 2.8], base_date=_TODAY - timedelta(days=10 + 2 * i))
            self.seed._make_signal(code, base_date, cycles=3)  # 信号段中段，窗口与段首重叠
        configs = {c['key']: c for c in scan.load_configs()}
        summary = review.review_pending(_TODAY, configs)
        self.assertEqual(summary['reviewed'], review.MIN_ADJ_SAMPLES)
        self.assertEqual(summary['adjustments'], [])
        self.assertEqual(DetectorDimConfig.objects.get(key='vol_ratio_slope').weight, 1.0)

    @override_settings(DETECTOR_AUTO_TUNE=True)
    def test_hits_matching_baseline_do_not_tune(self):
        from .detector import review, scan
        from .models import DetectorDimConfig

        # 历史稳步上行（做多基线≈1），信号也兑现：命中率≈基线，没有超额，不调参
        for i in range(review.MIN_ADJ_SAMPLES):
            code = f'6007{i}0'
            _s, base_date = self.seed._make_stock_with_moves(
                code, [0.5, 1.2, 2.2, 2.5, 2.8], history='up',
                base_date=_TODAY - timedelta(days=10 + 2 * i))
            self.seed._make_signal(code, base_date)
        configs = {c['key']: c for c in scan.load_configs()}
        summary = review.review_pending(_TODAY, configs)
        self.assertEqual(summary['hits'], review.MIN_ADJ_SAMPLES)
        self.assertEqual(summary['adjustments'], [])
        self.assertEqual(DetectorDimConfig.objects.get(key='vol_ratio_slope').weight, 1.0)

    def test_sector_review_needs_full_window_and_uses_next_close(self):
        from .detector import review
        from .models import DetectorSignal, MarketDailySnapshot

        # 板块基期为 T+1 收盘（#12）：需要 T+1..T+6 共 6 个交易日的快照
        base_date = _TODAY - timedelta(days=10)
        pcts = [(-0.3 if i % 2 else -0.1) for i in range(40)] + [0.0] + [0.5, 0.6, 0.4, 0.3, 0.2, 0.1]
        start = base_date - timedelta(days=40)
        for i, pct in enumerate(pcts):
            MarketDailySnapshot.objects.create(
                kind=MarketDailySnapshot.KIND_INDUSTRY_FF, trade_date=start + timedelta(days=i),
                payload=[{'name': '甲', 'net': 1e8, 'change_pct': pct}])
        sig = DetectorSignal.objects.create(
            target_kind='industry', target_code='甲', target_name='甲', trade_date=base_date,
            direction='up', cycles=2, status=DetectorSignal.STATUS_ACTIVE)
        out = review._sector_outcome(sig, {})
        self.assertIsNotNone(out)
        self.assertTrue(out['hit'])
        # 删掉窗口内一天的快照 → 窗口不连续，保持待回看
        MarketDailySnapshot.objects.filter(trade_date=base_date + timedelta(days=3)).delete()
        self.assertIsNone(review._sector_outcome(sig, {}))


@patch('stocks.trading_calendar.is_trading_day', _always_trading)
class DetectorRunDeferTests(TestCase):
    """PERF-09：只读端点的 DetectorRun 查询不加载 matrix 等大 JSON。"""

    def setUp(self):
        from .models import DetectorRun

        DetectorRun.objects.create(trade_date=_TODAY, matrix={'industry': [{'code': 'x'}] * 50},
                                   degraded=[{'target': 'a', 'reason': 'b'}], adjustments=[])

    def _run_selects(self, url):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        return [q['sql'] for q in ctx.captured_queries
                if 'detectorrun' in q['sql'].lower() and q['sql'].lstrip().upper().startswith('SELECT')]

    def test_signals_and_config_do_not_select_matrix(self):
        from django.urls import reverse

        for name in ('detector-signals', 'detector-config'):
            selects = self._run_selects(reverse(name))
            self.assertTrue(selects, name)
            for sql in selects:
                self.assertNotIn('"matrix"', sql, name)
                self.assertNotIn('"adjustments"', sql, name)


@patch('stocks.trading_calendar.is_trading_day', _always_trading)
class ConfigSingleSourceTests(TestCase):
    """ALG-15：配置页展示的回看/平静/分级规则直接来自实现常量，没有副本。"""

    def test_review_rules_follow_review_constants(self):
        from django.urls import reverse

        from .detector import constants, review

        with patch.object(constants, 'MIN_ADJ_SAMPLES', 42), patch.object(constants, 'HIT_PCT', 9.9):
            data = self.client.get(reverse('detector-config')).json()
        self.assertEqual(data['review_rules']['min_samples'], 42)
        self.assertEqual(data['review_rules']['hit_pct'], 9.9)
        self.assertEqual(data['review_rules']['lookback_days'], review.REVIEW_LOOKBACK)
        self.assertEqual(data['calm_rules']['price'], calm.PRICE_CALM_PARAMS)
        self.assertIn('severity', data['vote_rules'])

    def test_rvol_window_comes_from_calm_params(self):
        volumes = [100.0] * 30 + [300.0] * 5 + [100.0] * 15
        ctx = _price_ctx(volumes, [10.0] * 50)
        ok, meta = calm.is_calm_price(ctx, {'rvol_window': 20})
        # 前 20 日均量 = (15*100 + 5*300)/20 = 150 → RVOL = 0.667 < 0.7，不平静
        self.assertFalse(ok)
        self.assertIn('RVOL=0.67', meta['reason'])


@patch('stocks.trading_calendar.is_trading_day', _always_trading)
class SignalParamsSnapshotTests(TestCase):
    """ALG-15：信号行保存当日参数快照，详情曲线用快照重算而不是当前参数。"""

    def test_payload_keeps_params_and_detail_uses_snapshot(self):
        from django.urls import reverse

        from .detector import signals
        from .models import DetectorDimConfig

        def ev(ctx, params):
            return {'available': True, 'value': 30.0, 'direction': 'up', 'note': '', 'series': []}

        configs = [{'key': k, 'name': k, 'weight': 1.0, 'params': {'min': 0.123},
                    'targets': ('industry',), 'evaluate': ev, 'enabled': True, 'defaults': {}}
                   for k in ('ma_relation', 'big_order_net')]
        ctx = {'kind': 'industry', 'code': '甲', 'name': '甲', 'as_of': _TODAY.isoformat()}
        data, dims = signals.evaluate_target(ctx, configs)
        sig, _ = signals.upsert_signal(data, dims)
        self.assertEqual(sig.dimensions['ma_relation']['params'], {'min': 0.123})

        # 之后自动调参把 min 改了：详情重算仍用信号当日的 0.123
        DetectorDimConfig.objects.create(key='ma_relation', name='x', params={'min': 9.0})
        seen = []

        def spy(ctx, params):
            seen.append(params['min'])
            return {'available': True, 'value': 0, 'direction': None, 'note': '', 'series': []}

        from .detector import registry, views
        fake_ctx = ({'kind': 'industry', 'code': '甲', 'dates': [], 'change_pct': [], 'net': []}, '')
        with patch.dict(registry.DIMENSION_MAP, {'ma_relation': {**registry.DIMENSION_MAP['ma_relation'],
                                                                'evaluate': spy}}), \
                patch.object(views, '_context_for_signal', return_value=fake_ctx):
            resp = self.client.get(reverse('detector-signal-detail', args=[sig.pk])).json()
        self.assertEqual(seen, [0.123])
        self.assertEqual(resp['params_source']['ma_relation'], 'signal')

    def test_legacy_row_without_snapshot_falls_back_to_current(self):
        from django.urls import reverse

        from .detector import views
        from .models import DetectorSignal

        sig = DetectorSignal.objects.create(
            target_kind='industry', target_code='乙', target_name='乙', trade_date=_TODAY,
            direction='up', dimensions={'ma_relation': {'direction': 'up', 'available': True}})
        fake_ctx = ({'kind': 'industry', 'code': '乙', 'dates': [], 'change_pct': [], 'net': []}, '')
        with patch.object(views, '_context_for_signal', return_value=fake_ctx):
            resp = self.client.get(reverse('detector-signal-detail', args=[sig.pk])).json()
        self.assertEqual(resp['params_source']['ma_relation'], 'current')


@patch('stocks.trading_calendar.is_trading_day', _always_trading)
class AlertRuleSemanticsTests(TestCase):
    """ALG-14：上穿/下穿按昨收与今价是否跨过阈值判断；涨跌幅两侧对称；is_active 布尔解析。"""

    def setUp(self):
        from datetime import datetime, time
        from decimal import Decimal

        from .models import Stock

        self.Decimal, self.datetime, self.time = Decimal, datetime, time
        self.stock = Stock.objects.create(code='600000', name='浦发银行')

    def _daily(self, day, close, pct=None):
        from .models import DailyQuote

        D = self.Decimal
        return DailyQuote.objects.create(
            stock=self.stock, trade_date=day, open_price=D(str(close)), close_price=D(str(close)),
            high_price=D(str(close)), low_price=D(str(close)), open_close_diff=D('0'),
            open_close_pct=D('0'), high_low_diff=D('0'), high_low_pct=D('0'),
            change_pct=None if pct is None else D(str(pct)))

    def _bar(self, price):
        from .models import MinuteBar

        D = self.Decimal
        MinuteBar.objects.create(
            stock=self.stock, datetime=timezone.make_aware(self.datetime.combine(_TODAY, self.time(10, 0))),
            open=D(str(price)), close=D(str(price)), high=D(str(price)), low=D(str(price)), volume=100)

    def _eval(self):
        from . import alerts

        return alerts.evaluate_alerts(
            now=timezone.make_aware(self.datetime.combine(_TODAY, self.time(10, 5))))

    def _alert(self, rule, threshold):
        from .models import PriceAlert

        return PriceAlert.objects.create(stock=self.stock, rule_type=getattr(PriceAlert, rule),
                                         threshold=self.Decimal(str(threshold)))

    def test_price_above_does_not_repeat_when_already_above_yesterday(self):
        self._daily(_TODAY - timedelta(days=1), 10.6)
        self._alert('PRICE_ABOVE', 10.5)
        self._bar(10.8)
        self.assertEqual(self._eval(), [])  # 昨收已在阈值上方：不是穿越，旧实现每天都触发

    def test_price_above_fires_on_actual_cross(self):
        self._daily(_TODAY - timedelta(days=1), 10.4)
        alert = self._alert('PRICE_ABOVE', 10.5)
        self._bar(10.6)
        self.assertEqual([e.alert_id for e in self._eval()], [alert.id])

    def test_price_below_symmetric(self):
        self._daily(_TODAY - timedelta(days=1), 9.4)
        self._alert('PRICE_BELOW', 9.5)
        self._bar(9.3)
        self.assertEqual(self._eval(), [])
        self.stock.daily_quotes.all().delete()
        self._daily(_TODAY - timedelta(days=1), 9.6)
        self.assertEqual(len(self._eval()), 1)

    def test_no_previous_close_falls_back_to_level(self):
        alert = self._alert('PRICE_ABOVE', 10.5)
        self._bar(10.6)
        self.assertEqual([e.alert_id for e in self._eval()], [alert.id])

    def test_pct_above_uses_absolute_threshold(self):
        # 阈值误存为 -2：旧实现「涨幅 ≥ -2%」几乎每天触发；现按 |−2| 判断
        self._daily(_TODAY, 10.0, pct=-1.0)
        self._alert('DAILY_PCT_ABOVE', -2)
        self.assertEqual(self._eval(), [])


@patch('stocks.trading_calendar.is_trading_day', _always_trading)
class AlertApiParsingTests(TestCase):
    """ALG-14：创建时阈值统一量化（涨跌幅存绝对值）；is_active 按布尔口径解析。"""

    def setUp(self):
        from decimal import Decimal

        from .models import Stock

        self.Decimal = Decimal
        self.stock = Stock.objects.create(code='600000', name='浦发银行')

    def _alert(self, rule, threshold):
        from .models import PriceAlert

        return PriceAlert.objects.create(stock=self.stock, rule_type=getattr(PriceAlert, rule),
                                         threshold=self.Decimal(str(threshold)))

    def test_create_quantizes_and_normalizes_threshold(self):
        import json

        from .models import PriceAlert

        resp = self.client.post('/api/alerts/', json.dumps({
            'stock_id': self.stock.id, 'rule_type': PriceAlert.DAILY_PCT_ABOVE,
            'threshold': -3.123456}), content_type='application/json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(PriceAlert.objects.get().threshold, self.Decimal('3.1235'))
        resp = self.client.post('/api/alerts/', json.dumps({
            'stock_id': self.stock.id, 'rule_type': PriceAlert.PRICE_ABOVE,
            'threshold': 'NaN'}), content_type='application/json')
        self.assertEqual(resp.status_code, 400)

    def test_is_active_parses_string_false(self):
        import json

        alert = self._alert('PRICE_ABOVE', 10)
        resp = self.client.patch(f'/api/alerts/{alert.id}/', json.dumps({'is_active': 'false'}),
                                 content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        alert.refresh_from_db()
        self.assertFalse(alert.is_active)
        resp = self.client.patch(f'/api/alerts/{alert.id}/', json.dumps({'is_active': 'maybe'}),
                                 content_type='application/json')
        self.assertEqual(resp.status_code, 400)


class GroupListQueryTests(TestCase):
    """PERF-08：分组列表一次查询带出各组活跃股票数（不再每组一次 count）。"""

    def test_group_counts_in_constant_queries(self):
        from .models import Stock, StockGroup

        for g in range(5):
            group = StockGroup.objects.create(name=f'组{g}', order=g)
            for i in range(3):
                Stock.objects.create(code=f'60{g}{i:03d}', name='x', group=group, is_active=i != 0)
        with self.assertNumQueries(1):
            data = self.client.get('/api/stock-groups/').json()
        self.assertEqual([row['stock_count'] for row in data], [2] * 5)


def _screener_bars(stock, closes, start, highs=None, lows=None, is_final=True):
    from decimal import Decimal

    from .models import DailyQuote

    for i, close in enumerate(closes):
        c = Decimal(str(close))
        h = Decimal(str(highs[i])) if highs else c
        lo = Decimal(str(lows[i])) if lows else c
        DailyQuote.objects.create(
            stock=stock, trade_date=start + timedelta(days=i), open_price=c, close_price=c,
            high_price=h, low_price=lo, open_close_diff=Decimal('0'), open_close_pct=Decimal('0'),
            high_low_diff=Decimal('0'), high_low_pct=Decimal('0'), volume=100000,
            turnover=Decimal('1000000'), change_pct=Decimal('1'), is_final=is_final)


@patch('stocks.ai.valuation.fetch_valuation_map', new=lambda codes: {})
class ScreenerCrossSectionTests(TestCase):
    """ALG-12/PERF-08：只用同一交易日（as_of）的行情横截面筛选，查询批量化。"""

    def setUp(self):
        from datetime import date

        from .models import Stock

        self.start = date(2026, 6, 1)
        self.a = Stock.objects.create(code='600001', name='甲')
        self.b = Stock.objects.create(code='600002', name='乙')

    def test_stale_stock_is_excluded_and_reported(self):
        from .screener.engine import run_screener_with_meta

        _screener_bars(self.a, [10.0] * 30, self.start)
        _screener_bars(self.b, [10.0] * 25, self.start)  # 停在 5 天前
        rows, meta = run_screener_with_meta({'logic': 'all', 'conditions': [
            {'field': 'close_price', 'op': 'gt', 'value': 0}]})
        self.assertEqual([r['code'] for r in rows], ['600001'])
        self.assertEqual(meta['as_of'], (self.start + timedelta(days=29)).isoformat())
        self.assertEqual([e['code'] for e in meta['excluded']], ['600002'])
        self.assertEqual(meta['excluded'][0]['last_trade_date'],
                         (self.start + timedelta(days=24)).isoformat())

    def test_api_returns_as_of_and_excluded(self):
        import json

        _screener_bars(self.a, [10.0] * 3, self.start)
        resp = self.client.post('/api/screener/', json.dumps({'logic': 'all', 'conditions': [
            {'field': 'close_price', 'op': 'gt', 'value': 0}]}), content_type='application/json')
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        self.assertEqual(data['as_of'], (self.start + timedelta(days=2)).isoformat())
        self.assertEqual([e['code'] for e in data['excluded']], ['600002'])

    def test_queries_do_not_grow_with_stock_count(self):
        from .screener.engine import _latest_contexts
        from .models import Stock

        _screener_bars(self.a, [10.0] * 30, self.start)
        _screener_bars(self.b, [10.0] * 30, self.start)
        with self.assertNumQueries(4):
            ctxs, _ = _latest_contexts()
        self.assertEqual(len(ctxs), 2)
        for i in range(3, 8):
            _screener_bars(Stock.objects.create(code=f'60000{i}', name='x'), [10.0] * 30, self.start)
        with self.assertNumQueries(4):
            ctxs, _ = _latest_contexts()
        self.assertEqual(len(ctxs), 7)
        self.assertEqual(len(ctxs[0][1]['bars']), 30)


@patch('stocks.ai.valuation.fetch_valuation_map', new=lambda codes: {})
class ScreenerIndicatorConventionTests(TestCase):
    """ALG-12：MACD 预热 ≥120 根、BOLL 样本标准差、新高新低看最高/最低价且并列不算。"""

    def setUp(self):
        from datetime import date

        from .models import Stock

        self.start = date(2026, 1, 1)
        self.stock = Stock.objects.create(code='600010', name='指标')

    def _values(self):
        from .screener.engine import FIELDS, _latest_contexts

        ctxs, _ = _latest_contexts()
        ctx = ctxs[0][1]
        return {k: fn(ctx) for k, (_, fn, _) in FIELDS.items()}

    def test_macd_needs_120_bars(self):
        _screener_bars(self.stock, [10.0 + i * 0.01 for i in range(60)], self.start)
        self.assertIsNone(self._values()['macd_dif'])
        _screener_bars(self.stock, [10.6 + i * 0.01 for i in range(60)], self.start + timedelta(days=60))
        self.assertIsNotNone(self._values()['macd_dif'])

    def test_boll_uses_sample_std(self):
        from .screener import engine as screener

        # 取 x 使两种口径结论不同：总体口径（÷N）上轨更低 → 触及；
        # 样本口径（÷N-1，通达信 STD）上轨更高 → 不触及
        closes = [9.8, 10.2] * 9 + [10.0, 10.455]
        _screener_bars(self.stock, closes, self.start)
        mid = sum(closes) / 20
        pop = (sum((c - mid) ** 2 for c in closes) / 20) ** 0.5
        samp = (sum((c - mid) ** 2 for c in closes) / 19) ** 0.5
        self.assertTrue(mid + 2 * pop <= closes[-1] < mid + 2 * samp)
        self.assertEqual(screener._BOLL_DDOF, 1)
        self.assertEqual(self._values()['above_boll_upper'], 0)

    def test_new_high_uses_high_price_and_ties_are_not_new(self):
        closes = [10.0] * 20
        highs = [10.5] * 19 + [10.6]
        _screener_bars(self.stock, closes, self.start, highs=highs)
        values = self._values()
        self.assertEqual(values['new_high_20d'], 1)  # 收盘持平，但最高价创新高

    def test_tie_with_previous_high_is_not_new_high(self):
        closes = [10.0] * 19 + [10.5]
        highs = [10.5] * 20
        _screener_bars(self.stock, closes, self.start, highs=highs)
        self.assertEqual(self._values()['new_high_20d'], 0)


@patch('stocks.ai.valuation.fetch_valuation_map', new=lambda codes: {})
class ScreenerSortTests(TestCase):
    """ALG-12：排序字段无值的行无论升序还是降序都排在最后。"""

    def test_none_sorted_last_both_directions(self):
        from datetime import date

        from .screener.engine import run_screener
        from .models import Stock

        start = date(2026, 1, 1)
        long_ = Stock.objects.create(code='600001', name='长')
        short = Stock.objects.create(code='600002', name='短')
        _screener_bars(long_, [10.0 + i * 0.1 for i in range(30)], start)
        _screener_bars(short, [10.0, 10.0], start + timedelta(days=28))  # rsi_14 无值
        for direction in ('asc', 'desc'):
            rows = run_screener({'logic': 'all', 'conditions': [
                {'field': 'close_price', 'op': 'gt', 'value': 0}],
                'order_by': 'rsi_14', 'order_dir': direction})
            self.assertEqual([r['code'] for r in rows], ['600001', '600002'], direction)


class BuiltinStrategyConsistencyTests(TestCase):
    """ALG-16：内置模板条件与描述一致。"""

    def _spec(self, name):
        from .screener.strategies import BUILTIN_STRATEGIES

        return next(s['spec'] for s in BUILTIN_STRATEGIES if s['name'] == name)

    def _match_all(self, spec, values):
        from .screener.engine import _match

        return all(_match(c['op'], c['value'], values[c['field']]) for c in spec['conditions'])

    def test_low_pe_bluechip_excludes_zero_pe_and_small_caps(self):
        spec = self._spec('低估值蓝筹')
        good = {'pe_ttm': 8.0, 'pb': 1.0, 'total_mv': 1e11}
        self.assertTrue(self._match_all(spec, good))
        self.assertFalse(self._match_all(spec, {**good, 'pe_ttm': 0.0}))
        self.assertTrue(self._match_all(spec, {**good, 'total_mv': 3e10}))
        self.assertFalse(self._match_all(spec, {**good, 'total_mv': 2.9e10}))

    def test_pullback_requires_actual_drawdown(self):
        spec = self._spec('缩量回调不破20日线')
        base = {'above_ma20': 1, 'volume_ratio': 0.5}
        self.assertFalse(self._match_all(spec, {**base, 'pct_from_high_20d': 0.0}))
        self.assertTrue(self._match_all(spec, {**base, 'pct_from_high_20d': -5.0}))
