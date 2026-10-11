"""探测器测试：平静判定、同向投票、周期维持、回看调参、快照落库、API 降级。

约定与 tests_features 一致：交易日历 patch 为「每天都交易日」保持确定性；
所有上游接口（指数日线/两融）一律 patch，测试绝不触网。
"""

from datetime import datetime, time as dt_time, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .detector import calm, registry, review, scan, signals
from .detector.dimensions import _chain_return, _percentile, _sign_value, _std
from .models import (
    DailyQuote,
    DetectorDimConfig,
    DetectorRun,
    DetectorSignal,
    MarketDailySnapshot,
    Stock,
)

_TODAY = timezone.localdate()


def _trading_true(day=None):
    return True


def _after_close_clock():
    """只替换 scan 模块里的 timezone，把「现在」固定在今天 16:00。

    run_scan 对当日有 15:30 前跳过的守卫；不固定时钟的话，这类用例在 15:30 前跑必然拿到
    {'skipped': 'before_close'}（与墙钟相关的不稳定用例）。
    """
    fixed = timezone.make_aware(datetime.combine(_TODAY, dt_time(16, 0)))
    return SimpleNamespace(localdate=lambda *a: fixed.date(), localtime=lambda *a: fixed)


def _price_ctx(volumes, closes, kind='stock'):
    """构造指数/个股形态的 ctx；high/low 用 close±1% 生成（振幅恒定 2%）。"""
    dates = [(_TODAY - timedelta(days=len(volumes) - 1 - i)).isoformat() for i in range(len(volumes))]
    return {
        'kind': kind,
        'code': '000001' if kind == 'stock' else 'sh000001',
        'name': '测试',
        'dates': dates,
        'open': closes,
        'high': [c * 1.01 for c in closes],
        'low': [c * 0.99 for c in closes],
        'close': closes,
        'volume': volumes,
        'as_of': dates[-1],
    }


class RegistryIntegrityTests(TestCase):
    def test_enabled_dims_have_implementation_and_disabled_have_reason(self):
        for spec in registry.DIMENSIONS:
            if spec['enabled']:
                self.assertTrue(spec['evaluate'], f"{spec['key']} 启用但无实现")
                self.assertTrue(spec['targets'], f"{spec['key']} 启用但无适用对象")
            else:
                # 数据诚信红线：停用维度必须写明原因，不允许沉默缺失
                self.assertTrue(spec['disabled_reason'], f"{spec['key']} 停用但未说明原因")

    def test_disabled_dims_are_exactly_the_sourceless_two(self):
        keys = {s['key'] for s in registry.DIMENSIONS if not s['enabled']}
        self.assertEqual(keys, {'order_imbalance', 'sector_dispersion'})


class MathHelperTests(TestCase):
    def test_sign_value_bands(self):
        params = {'mild': 1.0, 'min': 0.1, 'max': 2.0}
        _v, up, _n = _sign_value(0.5, params)
        self.assertEqual(up, 'up')
        _v, none_weak, _n = _sign_value(0.05, params)
        self.assertIsNone(none_weak)
        _v, none_violent, note = _sign_value(3.0, params)
        self.assertIsNone(none_violent)
        self.assertIn('突变', note)

    def test_chain_return_compounds(self):
        # (1.01*1.01-1)*100 ≈ 2.01
        self.assertAlmostEqual(_chain_return([1.0, 1.0], 2), 2.01, places=2)

    def test_std_and_percentile_ignore_none_and_require_data(self):
        self.assertIsNone(_std([None]))
        self.assertEqual(_percentile([1, 2, 3, 4], 0.5), 2.5)


class CalmPriceTests(TestCase):
    def test_flat_volume_and_low_amplitude_is_calm(self):
        # 历史量能带微小噪声：完全恒定的合成量会让 CV 分位=0，任何扰动都「未收窄」，
        # 现实数据不存在绝对恒定，用 ±2 的交替噪声模拟常态
        volumes = [100 + (2 if i % 2 == 0 else -2) for i in range(40)]
        volumes += [99.0, 101.0, 100.0, 100.5, 100.2]
        closes = [10.0] * 45
        ctx = _price_ctx(volumes, closes)
        is_calm, metrics = calm.is_calm_price(ctx)
        self.assertTrue(is_calm, metrics)
        self.assertIn('rvol', metrics)

    def test_volume_spike_breaks_calm(self):
        volumes = [100.0] * 44 + [100.0, 260.0]
        closes = [10.0] * 45
        ctx = _price_ctx(volumes, closes)
        is_calm, metrics = calm.is_calm_price(ctx)
        self.assertFalse(is_calm)
        self.assertIn('RVOL', metrics['reason'])

    def test_insufficient_history_is_not_calm(self):
        ctx = _price_ctx([100.0] * 10, [10.0] * 10)
        is_calm, metrics = calm.is_calm_price(ctx)
        self.assertFalse(is_calm)
        self.assertIn('不足', metrics['reason'])


class CalmSectorTests(TestCase):
    def _sector_ctx(self, nets, pcts):
        dates = [(_TODAY - timedelta(days=len(nets) - 1 - i)).isoformat() for i in range(len(nets))]
        return {'kind': 'industry', 'code': 'x', 'name': 'x', 'dates': dates,
                'net': nets, 'change_pct': pcts, 'as_of': dates[-1]}

    def test_quiet_flow_and_low_volatility_is_calm(self):
        nets = [3e8 if i % 2 == 0 else -3e8 for i in range(16)]
        pcts = [0.3 if i % 2 == 0 else -0.3 for i in range(16)]
        is_calm, metrics = calm.is_calm_sector(self._sector_ctx(nets, pcts))
        self.assertTrue(is_calm, metrics)

    def test_flow_surge_breaks_calm(self):
        nets = [3e8 if i % 2 == 0 else -3e8 for i in range(13)] + [30e8, 32e8, 31e8]
        pcts = [0.3 if i % 2 == 0 else -0.3 for i in range(16)]
        is_calm, metrics = calm.is_calm_sector(self._sector_ctx(nets, pcts))
        self.assertFalse(is_calm)
        self.assertIn('净额', metrics['reason'])


def _vote_config(key, direction_values):
    """构造投票用配置：evaluate 按预置值返回 direction，绕开真实维度计算。"""
    def _evaluate(ctx, params, _values=direction_values):
        value, direction = _values.get(ctx['code'], (0.0, None))
        return {'available': True, 'value': value, 'direction': direction,
                'note': '', 'series': []}

    return {
        'key': key, 'name': key, 'weight': 1.0, 'params': {'mild': 1.0, 'min': 0.1, 'max': 2.0},
        'targets': ('index', 'stock', 'industry', 'concept'),
        'evaluate': _evaluate, 'enabled': True, 'defaults': {},
    }


@patch('stocks.trading_calendar.is_trading_day', _trading_true)
class VoteTests(TestCase):
    def _ctx(self, code='600000'):
        return {'kind': 'stock', 'code': code, 'name': code, 'as_of': _TODAY.isoformat()}

    def test_two_same_direction_votes_fire_signal(self):
        configs = [
            _vote_config('a', {'600000': (30.0, 'up')}),
            _vote_config('b', {'600000': (40.0, 'up')}),
        ]
        signal_data, dims = signals.evaluate_target(self._ctx(), configs)
        self.assertIsNotNone(signal_data)
        self.assertEqual(signal_data['direction'], 'up')
        self.assertEqual(signal_data['cycles'], 1)
        self.assertEqual(signal_data['status'], DetectorSignal.STATUS_WATCHING)
        self.assertEqual(len(dims), 2)

    def test_single_vote_does_not_fire(self):
        configs = [
            _vote_config('a', {'600000': (30.0, 'up')}),
            _vote_config('b', {'600000': (0.0, None)}),
        ]
        signal_data, _dims = signals.evaluate_target(self._ctx(), configs)
        self.assertIsNone(signal_data)

    def test_opposite_vote_blocks_signal(self):
        # 有反向票说明维度间分歧，不构成方向性微变（从严口径）
        configs = [
            _vote_config('a', {'600000': (30.0, 'up')}),
            _vote_config('b', {'600000': (40.0, 'up')}),
            _vote_config('c', {'600000': (20.0, 'down')}),
        ]
        signal_data, _dims = signals.evaluate_target(self._ctx(), configs)
        self.assertIsNone(signal_data)

    def test_weighted_score_reflects_weights(self):
        configs = [
            _vote_config('a', {'600000': (50.0, 'up')}),
            _vote_config('b', {'600000': (50.0, 'up')}),
        ]
        configs[0]['weight'] = 3.0
        configs[1]['weight'] = 1.0
        signal_data, _dims = signals.evaluate_target(self._ctx(), configs)
        self.assertEqual(signal_data['score'], 50.0)


class CycleAndPersistenceTests(TestCase):
    def setUp(self):
        self.configs = [
            _vote_config('a', {'600000': (30.0, 'up')}),
            _vote_config('b', {'600000': (40.0, 'up')}),
        ]

    def _ctx(self, code='600000', offset_days=0):
        return {
            'kind': 'stock', 'code': code, 'name': code,
            'as_of': (_TODAY + timedelta(days=offset_days)).isoformat(),
        }

    def test_second_day_same_direction_becomes_active(self):
        with patch('stocks.trading_calendar.is_trading_day', _trading_true):
            first, _ = signals.evaluate_target(self._ctx(), self.configs)
            signals.upsert_signal(first, {})
            # 第二天的评估发生在下一个交易日（as_of 前移一天）
            second, _ = signals.evaluate_target(self._ctx(offset_days=1), self.configs)
        self.assertEqual(second['cycles'], 2)
        self.assertEqual(second['status'], DetectorSignal.STATUS_ACTIVE)

    def test_direction_flip_resets_cycles(self):
        with patch('stocks.trading_calendar.is_trading_day', _trading_true):
            configs_down = [
                _vote_config('a', {'600000': (30.0, 'down')}),
                _vote_config('b', {'600000': (40.0, 'down')}),
            ]
            day1, _ = signals.evaluate_target(self._ctx(), configs_down)
            signals.upsert_signal(day1, {})
            day2, _ = signals.evaluate_target(self._ctx(offset_days=1), self.configs)
        self.assertEqual(day2['cycles'], 1)
        self.assertEqual(day2['direction'], 'up')

    def test_upsert_is_idempotent(self):
        with patch('stocks.trading_calendar.is_trading_day', _trading_true):
            data, _ = signals.evaluate_target(self._ctx(), self.configs)
            signals.upsert_signal(data, {})
            signals.upsert_signal(data, {})
        self.assertEqual(DetectorSignal.objects.count(), 1)


class SectorSignalScanTests(TestCase):
    """端到端（板块路径）：工程化的快照序列应产出「价格组 + 资金组」两组同向的信号。

    序列设计（45 日：均线窗口 + 噪声闸门基线）：
    - 前 36 日：涨跌幅为 σ≈0.5% 的固定噪声、净额正负交替且幅度不等（各维度与
      平静闸门的「自身历史」基线）；
    - 尾 9 日：涨跌幅收窄为约 +0.25%/日的稳定上行 → 滞后窗口（止于 T−3）判
      「此前平静」，近 3 日累计约 0.76% < 2.5σ√3（价格尚未大动）；vol_compress
      投 up（价格组）；净额由 +1e8 抬到 +4e8（资金面温和流入，未超历史 95 分位）
      → big_order_net 投 up（资金组）。
    分组投票（#4）后 vol_compress 与 ma_relation 同属价格组只算一票，因此这里
    必须有资金组的独立一票才出信号。
    """

    def setUp(self):
        self.names = ['测试行业']
        hist = [
            -0.62, 0.48, 0.71, -0.35, -0.8, 0.12, 0.33, 0.66, -0.58, -0.71,
            0.41, 0.27, 0.83, 0.55, -0.22, -0.4, 0.9, -0.14, -0.52, -0.67,
            0.21, 0.32, -0.45, -0.11, 0.44, 0.5, 0.76, 0.13, -0.87, 0.51,
            -0.61, -0.33, -0.72, 0.1, -0.42, 0.23,
        ]
        self.pcts = hist + [0.24, 0.27, 0.25, 0.26, 0.24, 0.27, 0.25, 0.26, 0.25]
        mags = [2.0, 4.5, 3.0, 5.0, 2.5, 4.0, 3.5, 2.2, 4.8, 3.2]
        self.nets = [(mags[i % 10] if i % 2 == 0 else -mags[i % 10]) * 1e8 for i in range(len(hist))]
        self.nets += [1e8] * 6 + [3e8, 3.5e8, 4e8]

    def _seed_full_history(self):
        """把完整 30 日序列落成快照（板块宇宙取最近 3 日名字并集）。"""
        MarketDailySnapshot.objects.all().delete()
        n = len(self.pcts)
        for i in range(n):
            MarketDailySnapshot.objects.create(
                kind=MarketDailySnapshot.KIND_INDUSTRY_FF,
                trade_date=_TODAY - timedelta(days=n - 1 - i),
                payload=[{'name': nm, 'net': self.nets[i], 'change_pct': self.pcts[i]}
                         for nm in self.names],
            )

    def _run(self):
        with patch('stocks.trading_calendar.is_trading_day', _trading_true), \
                patch.object(scan, 'timezone', _after_close_clock()), \
                patch('stocks.detector.scan.save_margin_snapshot', return_value=0), \
                patch('stocks.detector.scan.build_index_context', return_value=(None, '指数日线不可用')):
            return scan.run_scan()

    def test_calm_sector_with_two_aligned_dims_fires_signal(self):
        self._seed_full_history()
        summary = self._run()
        self.assertEqual(summary['status'], 'partial')  # 指数源被 patch 掉，如实降级
        sig = DetectorSignal.objects.filter(target_kind='industry', target_code='测试行业').first()
        self.assertIsNotNone(sig, summary)
        self.assertEqual(sig.direction, 'up')
        # 周期按「方向组同向状态」回看（#2）：尾段此前几天价格组+资金组已同向上行，
        # 不要求那些天也平静、也落过信号行，所以首个信号日即为维持中
        self.assertGreater(sig.cycles, 1)
        self.assertEqual(sig.status, DetectorSignal.STATUS_ACTIVE)
        counted = {k for k, v in sig.dimensions.items() if v['counted']}
        self.assertEqual(counted, {'vol_compress', 'big_order_net'}, sig.dimensions)
        self.assertEqual({sig.dimensions[k]['group'] for k in counted}, {'price', 'funds'})
        # 热力图矩阵里有该板块且分值非空
        run = DetectorRun.objects.get(trade_date=_TODAY)
        rows = run.matrix['industry']
        self.assertTrue(rows and rows[0]['values']['vol_compress'] is not None)

    def test_volatile_sector_is_skipped(self):
        # 尾段波动一直放大：不满足平静 → 不评估、无信号
        self._seed_full_history()
        snaps = MarketDailySnapshot.objects.filter(
            kind=MarketDailySnapshot.KIND_INDUSTRY_FF,
        ).order_by('trade_date')
        for i, snap_row in enumerate(snaps):
            payload = snap_row.payload
            payload[0]['change_pct'] = 0.3 if i % 2 == 0 else -0.3
            if i >= 24:
                payload[0]['change_pct'] = (i - 22) * 0.6  # 尾段波动放大
            payload[0]['net'] = 3e8 if i % 2 == 0 else -3e8
            snap_row.save()
        self._run()
        self.assertFalse(
            DetectorSignal.objects.filter(target_kind='industry').exists()
        )


class ReviewTests(TestCase):
    """回看与调参：兑现推进状态、权重有界调整、样本不足不动参数。

    口径（ALG-05）：后续必须凑满 5 个交易日、基期为 T+1 开盘、阈值 k×σ20、
    命中率与同标的无条件基线比较。每个信号用独立的股票：
    - 信号日之前造 40 根「历史」日线，用来算 σ20 与基线：history='down' 为稳步
      下行（做多基线≈0，信号兑现即跑赢基线），'up' 为稳步上行（做多基线≈1）；
    - 信号日之后按 moves（相对信号日收盘的涨跌%）造后续日线，开盘=收盘。
    """

    def _make_stock_with_moves(self, code, moves, history='down'):
        """造自选股 + 日线。DailyQuote 的派生字段有 NOT NULL 约束，
        必须先算（compute_derived_fields）再存。moves 为信号日后逐日涨跌%。"""
        stock = Stock.objects.create(code=code, name=code)
        base_date = _TODAY - timedelta(days=10)

        def _quote(day, close, prev_close=None):
            q = DailyQuote(
                stock=stock, trade_date=day,
                open_price=round(close, 3), close_price=round(close, 3),
                high_price=round(close * 1.01, 3), low_price=round(close * 0.99, 3),
                volume=1000000,
            )
            q.prev_close = prev_close
            q.compute_derived_fields()
            q.save()
            return q

        # 历史：交替 -0.2%/-0.6%（或 +0.6%/+0.2%）的稳步漂移，σ20≈0.2
        steps = [-0.2, -0.6] if history == 'down' else [0.6, 0.2]
        closes = [10.0]
        for i in range(40):
            closes.append(closes[-1] / (1 + steps[i % 2] / 100))
        closes.reverse()  # closes[-1] == 10 落在信号日
        for i, close in enumerate(closes[:-1]):
            _quote(base_date - timedelta(days=len(closes) - 1 - i), close)
        _quote(base_date, 10)
        prev = 10
        for i, pct in enumerate(moves, start=1):
            close = 10 * (1 + pct / 100)
            _quote(base_date + timedelta(days=i), close, prev)
            prev = close
        return stock, base_date

    def _make_signal(self, code, base_date, direction='up', dims=None, cycles=2):
        return DetectorSignal.objects.create(
            target_kind='stock', target_code=code, target_name=code,
            trade_date=base_date, direction=direction, cycles=cycles, score=60,
            severity=DetectorSignal.SEVERITY_MEDIUM,
            status=DetectorSignal.STATUS_ACTIVE,
            dimensions=dims or {
                'vol_ratio_slope': {'name': '量比斜率', 'value': 30, 'direction': direction,
                                    'weight': 1.0, 'available': True, 'note': ''},
                'ma_relation': {'name': '价格与均线关系', 'value': 20, 'direction': direction,
                                'weight': 1.0, 'available': True, 'note': ''},
            },
        )

    def _seed_hit_signals(self, prefix, n=review.MIN_ADJ_SAMPLES):
        for i in range(n):
            _stock, base_date = self._make_stock_with_moves(f'6001{i}0', [0.5, 1.2, 2.2, 2.5, 2.8])
            self._make_signal(f'6001{i}0', base_date)
        return base_date

    def test_hit_confirms_and_adjusts_weight_up(self):
        self._seed_hit_signals('6001')
        configs = {c['key']: c for c in scan.load_configs()}
        with patch('stocks.trading_calendar.is_trading_day', _trading_true):
            summary = review.review_pending(_TODAY, configs)
        n = review.MIN_ADJ_SAMPLES
        self.assertEqual(summary['reviewed'], n)
        self.assertEqual(summary['hits'], n)
        self.assertEqual(summary['hit_rate'], 1.0)
        self.assertLess(summary['baseline_rate'], 0.2)
        for sig in DetectorSignal.objects.all():
            self.assertEqual(sig.status, DetectorSignal.STATUS_CONFIRMED)
            self.assertTrue(sig.review_hit)
        cfg = DetectorDimConfig.objects.get(key='vol_ratio_slope')
        self.assertGreater(cfg.weight, 1.0)

    def test_miss_marks_and_adjusts_weight_down(self):
        # 后续 5 日完整、一路下跌；历史稳步上行，做多基线≈1 → 明显跑输基线
        for i in range(review.MIN_ADJ_SAMPLES):
            _stock, base_date = self._make_stock_with_moves(
                f'6002{i}0', [-0.5, -1.0, -1.5, -1.8, -2.0], history='up')
            self._make_signal(f'6002{i}0', base_date)
        configs = {c['key']: c for c in scan.load_configs()}
        with patch('stocks.trading_calendar.is_trading_day', _trading_true):
            summary = review.review_pending(_TODAY, configs)
        self.assertEqual(summary['hits'], 0)
        self.assertEqual(summary['reviewed'], review.MIN_ADJ_SAMPLES)
        cfg = DetectorDimConfig.objects.get(key='vol_ratio_slope')
        self.assertLess(cfg.weight, 1.0)

    def test_weight_adjustment_is_bounded(self):
        for i in range(review.MIN_ADJ_SAMPLES):
            _stock, base_date = self._make_stock_with_moves(f'6003{i}0', [3.0, 3.0, 3.0, 3.5, 4.0])
            self._make_signal(f'6003{i}0', base_date)
        DetectorDimConfig.objects.create(
            key='vol_ratio_slope', name='量比斜率', weight=1.98,
            params={'window': 10, 'slope_days': 3, 'mild': 0.15, 'min': 0.02, 'max': 0.5},
        )
        configs = {c['key']: c for c in scan.load_configs()}
        with patch('stocks.trading_calendar.is_trading_day', _trading_true):
            summary = review.review_pending(_TODAY, configs)
        self.assertTrue(summary['adjustments'])
        cfg = DetectorDimConfig.objects.get(key='vol_ratio_slope')
        self.assertLessEqual(cfg.weight, 2.0)

    def test_single_sample_does_not_adjust(self):
        _stock, base_date = self._make_stock_with_moves('600004', [2.0, 2.1, 2.2, 2.3, 2.4])
        self._make_signal('600004', base_date)
        configs = {c['key']: c for c in scan.load_configs()}
        with patch('stocks.trading_calendar.is_trading_day', _trading_true):
            summary = review.review_pending(_TODAY, configs)
        self.assertEqual(summary['reviewed'], 1)
        cfg = DetectorDimConfig.objects.get(key='vol_ratio_slope')
        self.assertEqual(cfg.weight, 1.0)

    def test_missing_data_leaves_signal_pending(self):
        # 没有后续行情 → 不把「查不到」当「没兑现」，信号保持待回看
        _stock, base_date = self._make_stock_with_moves('600005', [])
        self._make_signal('600005', base_date)
        configs = {c['key']: c for c in scan.load_configs()}
        with patch('stocks.trading_calendar.is_trading_day', _trading_true):
            summary = review.review_pending(_TODAY, configs)
        self.assertEqual(summary['reviewed'], 0)
        self.assertEqual(
            DetectorSignal.objects.get().status, DetectorSignal.STATUS_ACTIVE
        )


class MarginSnapshotTests(TestCase):
    def test_writes_only_days_with_both_markets(self):
        from stocks.market import sentiment

        base = _TODAY - timedelta(days=3)
        sh_rows = {
            (base + timedelta(days=i)).strftime('%Y%m%d'): {
                'total': 19000.0 + i, 'rz': 18000.0 + i, 'rq': 1000.0,
            }
            for i in range(3)
        }
        with patch('stocks.trading_calendar.is_trading_day', _trading_true), \
                patch.object(sentiment, '_sh_margin_rows', return_value=sh_rows), \
                patch.object(sentiment, '_sz_margin_row', side_effect=[None, {'total': 13000.0, 'rz': 12000.0}, {'total': 13100.0, 'rz': 12100.0}]):
            from stocks.market.snapshots import save_margin_snapshot

            written = save_margin_snapshot()
        # 首个候选日深市未披露（T+1 预期），只写齐全的两天
        self.assertEqual(written, 2)
        self.assertEqual(
            MarketDailySnapshot.objects.filter(kind=MarketDailySnapshot.KIND_MARGIN).count(), 2
        )
        row = MarketDailySnapshot.objects.filter(
            kind=MarketDailySnapshot.KIND_MARGIN,
        ).order_by('trade_date').first()
        # 探测顺序：today-1 深市未披露跳过、today-2 → sh 19001+13000、
        # today-3 → sh 19000+13100；最早行 = 32100
        self.assertEqual(row.payload[0]['total'], 32100.0)

    def test_rerun_skips_existing_dates(self):
        from stocks.market import sentiment
        from stocks.market.snapshots import save_margin_snapshot

        base = _TODAY - timedelta(days=2)
        sh_rows = {
            (base + timedelta(days=i)).strftime('%Y%m%d'): {'total': 19000.0, 'rz': 18000.0, 'rq': 1000.0}
            for i in range(2)
        }
        with patch('stocks.trading_calendar.is_trading_day', _trading_true), \
                patch.object(sentiment, '_sh_margin_rows', return_value=sh_rows), \
                patch.object(sentiment, '_sz_margin_row', return_value={'total': 13000.0, 'rz': 12000.0}):
            save_margin_snapshot()
            with patch.object(sentiment, '_sz_margin_row') as probe:
                written = save_margin_snapshot()
        self.assertEqual(written, 0)
        probe.assert_not_called()  # 已落库日期不再探测（省请求）


class DegradedScanTests(TestCase):
    def test_all_sources_down_produces_no_fabricated_signals(self):
        with patch('stocks.trading_calendar.is_trading_day', _trading_true), \
                patch.object(scan, 'timezone', _after_close_clock()), \
                patch('stocks.detector.scan.save_margin_snapshot', return_value=0), \
                patch('stocks.detector.scan.build_index_context', return_value=(None, '指数日线不可用')):
            summary = scan.run_scan()
        self.assertEqual(summary['status'], 'partial')
        self.assertGreater(summary['degraded_count'], 0)
        self.assertEqual(DetectorSignal.objects.count(), 0)
        run = DetectorRun.objects.get()
        self.assertEqual(run.status, 'partial')
        self.assertTrue(run.degraded)

    def test_non_trading_day_is_skipped(self):
        with patch('stocks.trading_calendar.is_trading_day', return_value=False):
            summary = scan.run_scan()
        self.assertEqual(summary['skipped'], 'non_trading_day')
        self.assertEqual(DetectorRun.objects.count(), 0)


@patch('stocks.trading_calendar.is_trading_day', _trading_true)
class DetectorApiTests(TestCase):
    def setUp(self):
        self.run_row = DetectorRun.objects.create(
            trade_date=_TODAY, status='ok', targets_scanned=10, calm_count=5,
            matrix={'industry': [
                {'code': '半导体', 'name': '半导体', 'as_of': _TODAY.isoformat(),
                 'direction': 'up', 'score': 55,
                 'values': {'vol_compress': 42.0, 'ma_relation': 30.0}},
                {'code': '银行', 'name': '银行', 'as_of': _TODAY.isoformat(),
                 'direction': None, 'score': 0,
                 'values': {'vol_compress': None, 'ma_relation': -10.0}},
            ]},
        )
        self.signal = DetectorSignal.objects.create(
            target_kind='industry', target_code='半导体', target_name='半导体',
            trade_date=_TODAY, direction='up', cycles=2, score=55,
            severity=DetectorSignal.SEVERITY_HIGH, status=DetectorSignal.STATUS_ACTIVE,
            dimensions={'vol_compress': {'name': '波动率压缩', 'value': 42, 'direction': 'up',
                                         'weight': 1.0, 'available': True, 'note': ''}},
        )

    def test_signal_list_defaults_to_latest_run_date(self):
        DetectorSignal.objects.create(
            target_kind='index', target_code='sh000001', target_name='上证指数',
            trade_date=_TODAY - timedelta(days=3), direction='up', cycles=1,
            score=30, severity=DetectorSignal.SEVERITY_LOW,
            status=DetectorSignal.STATUS_WATCHING, dimensions={},
        )
        resp = self.client.get(reverse('detector-signals'))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body['summary']['total'], 1)  # 默认只看最近扫描日
        self.assertEqual(body['items'][0]['id'], self.signal.id)

    def test_severity_filter_and_invalid_value(self):
        resp = self.client.get(reverse('detector-signals'), {'severity': 'high'})
        self.assertEqual(resp.json()['summary']['total'], 1)
        resp = self.client.get(reverse('detector-signals'), {'severity': 'bad'})
        self.assertEqual(resp.status_code, 400)

    def test_heatmap_returns_matrix_and_caps(self):
        resp = self.client.get(reverse('detector-heatmap'), {'board': 'industry'})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body['rows']), 2)
        self.assertTrue(body['rows'][0]['values']['vol_compress'] is not None)
        self.assertTrue(
            all(d['key'] in registry.ENABLED_KEYS for d in body['dims'])
        )
        resp = self.client.get(reverse('detector-heatmap'), {'board': 'bad'})
        self.assertEqual(resp.status_code, 400)

    def test_config_exposes_disabled_dims_with_reason(self):
        resp = self.client.get(reverse('detector-config'))
        body = resp.json()
        disabled = {d['key'] for d in body['dims'] if not d['enabled']}
        self.assertEqual(disabled, {'order_imbalance', 'sector_dispersion'})
        for dim in body['dims']:
            if not dim['enabled']:
                self.assertTrue(dim['disabled_reason'])

    def test_signal_detail_returns_404_for_missing(self):
        resp = self.client.get(reverse('detector-signal-detail', args=[9999]))
        self.assertEqual(resp.status_code, 404)
