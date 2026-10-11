"""calm-vote 方向回归测试：分组加权投票（#4）、权重参与投票、周期按维度状态（#2）、
多重检验报告与按强度截断（#11）。

约定与 tests_detector 一致：交易日历 patch 为确定值，上游接口一律 patch，测试不触网。
"""

import math
import random
from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from .detector import calm, registry, scan, signals
from .detector.constants import OPPOSE_RATIO, VOTE_MIN_WEIGHT
from .models import DetectorRun, DetectorSignal

_TODAY = timezone.localdate()


def _always_trading(day=None):
    return True


def _res(direction, value=50.0, weight=1.0, available=True):
    return {'available': available, 'direction': direction, 'value': value, 'weight': weight}


def _cfg(key, direction=None, value=50.0, weight=1.0, targets=('index', 'stock', 'industry', 'concept')):
    def _evaluate(ctx, params):
        return {'available': True, 'value': value, 'direction': direction, 'note': '', 'series': []}

    return {'key': key, 'name': key, 'weight': weight, 'params': {}, 'targets': targets,
            'evaluate': _evaluate, 'enabled': True, 'defaults': {}}


class GroupedVoteTests(SimpleTestCase):
    """#4：方向组决定方向（组内取强），量能强度组只确认、不定方向、不否决。"""

    def test_registry_declares_groups(self):
        groups = {s['key']: (s.get('group'), s.get('role')) for s in registry.DIMENSIONS if s['enabled']}
        self.assertEqual(groups['vol_compress'], ('price', 'direction'))
        self.assertEqual(groups['ma_relation'], ('price', 'direction'))
        self.assertEqual(groups['big_order_net'], ('funds', 'direction'))
        self.assertEqual(groups['margin_north'], ('leverage', 'direction'))
        self.assertEqual(groups['vol_ratio_slope'], ('volume', 'strength'))
        self.assertEqual(groups['turnover_pct'], ('volume', 'strength'))

    def test_same_group_counts_once(self):
        # 价格组两个维度同向：只算一票，不能凑出信号（旧口径同一条价格序列算两票）
        v = signals.vote({'vol_compress': _res('up'), 'ma_relation': _res('up')})
        self.assertIsNone(v['direction'])
        self.assertEqual(v['n_groups'], 1)

    def test_volume_alone_cannot_set_direction(self):
        # 两个量能维度来自同一条成交量序列：没有方向组时不出信号
        v = signals.vote({'vol_ratio_slope': _res('up'), 'turnover_pct': _res('up')})
        self.assertIsNone(v['direction'])

    def test_volume_rise_confirms_price_direction_either_way(self):
        for side in ('up', 'down'):
            v = signals.vote({'vol_compress': _res(side), 'vol_ratio_slope': _res('up')})
            self.assertEqual(v['direction'], side)
            self.assertEqual(set(v['counted']), {'vol_compress', 'vol_ratio_slope'})

    def test_volume_shrink_neither_confirms_nor_vetoes(self):
        # 缩量 down 不再被当成价格向下的一票（审查：个股信号几乎全向下的来源）
        v = signals.vote({'vol_compress': _res('down'), 'vol_ratio_slope': _res('down'),
                          'turnover_pct': _res('down')})
        self.assertIsNone(v['direction'])
        v = signals.vote({'vol_compress': _res('up'), 'big_order_net': _res('up'),
                          'vol_ratio_slope': _res('down')})
        self.assertEqual(v['direction'], 'up')

    def test_intra_group_conflict_nets_out_by_strength(self):
        v = signals.vote({'vol_compress': _res('up', 80.0), 'ma_relation': _res('down', 30.0),
                          'big_order_net': _res('up')})
        self.assertEqual(v['direction'], 'up')
        self.assertEqual(v['groups']['price']['rep'], 'vol_compress')

    def test_opposite_side_ratio_instead_of_zero_votes(self):
        # 三组同向、一组反向：反向加权票 1 < 3×OPPOSE_RATIO，不再被一票否决
        dims = {'a': _res('up'), 'b': _res('up'), 'c': _res('up'), 'd': _res('down')}
        self.assertLess(1.0, 3 * OPPOSE_RATIO)
        self.assertEqual(signals.vote(dims)['direction'], 'up')
        # 两组同向、一组反向：1 ≥ 2×0.5，分歧，不出信号
        dims.pop('c')
        self.assertIsNone(signals.vote(dims)['direction'])

    def test_weight_participates_in_vote(self):
        # 两组都被调参降到 0.5：加权票 1.0 < VOTE_MIN_WEIGHT，不出信号
        dims = {'vol_compress': _res('up', weight=0.5), 'big_order_net': _res('up', weight=0.5)}
        self.assertLess(1.0, VOTE_MIN_WEIGHT)
        self.assertIsNone(signals.vote(dims)['direction'])
        # 其中一组权重升到 1.0：1.5 ≥ VOTE_MIN_WEIGHT，出信号
        dims['big_order_net']['weight'] = 1.0
        self.assertEqual(signals.vote(dims)['direction'], 'up')
        # 权重也决定对侧比例：方向组 2.0 对 0.9 → 0.9 < 1.0，出信号；对 1.0 → 分歧
        dims = {'vol_compress': _res('up', weight=2.0), 'big_order_net': _res('down', weight=0.9),
                'vol_ratio_slope': _res('up')}
        self.assertEqual(signals.vote(dims)['direction'], 'up')
        dims['big_order_net']['weight'] = 1.0
        self.assertIsNone(signals.vote(dims)['direction'])

    def test_single_heavy_group_cannot_fire_alone(self):
        v = signals.vote({'vol_compress': _res('up', weight=2.0)})
        self.assertIsNone(v['direction'])

    def test_unknown_keys_are_own_direction_groups(self):
        v = signals.vote({'x': _res('up'), 'y': _res('up')})
        self.assertEqual(v['direction'], 'up')

    def test_severity_high_reachable_with_all_available_groups(self):
        # 个股最多 2 组（价格 + 量能确认）：全部可用组同向 + 维持 + 高分 = 高
        self.assertEqual(signals._severity(60.0, 2, 2, groups_available=2), DetectorSignal.SEVERITY_HIGH)
        self.assertEqual(signals._severity(60.0, 2, 2, groups_available=4), DetectorSignal.SEVERITY_MEDIUM)
        self.assertEqual(signals._severity(60.0, 2, 3), DetectorSignal.SEVERITY_HIGH)


@patch('stocks.trading_calendar.is_trading_day', _always_trading)
class EvaluateTargetGroupingTests(TestCase):
    def _ctx(self):
        return {'kind': 'stock', 'code': '600000', 'name': 'x', 'as_of': _TODAY.isoformat()}

    def test_payload_marks_group_role_and_counted(self):
        configs = [_cfg('vol_compress', 'up', 60.0), _cfg('ma_relation', 'up', 40.0),
                   _cfg('vol_ratio_slope', 'up', 30.0), _cfg('turnover_pct', 'down', 20.0)]
        data, dims = signals.evaluate_target(self._ctx(), configs)
        self.assertEqual(data['direction'], 'up')
        payload = signals.dimensions_payload(dims)
        self.assertTrue(payload['vol_compress']['counted'])
        self.assertFalse(payload['ma_relation']['counted'])  # 同组非代表
        self.assertTrue(payload['vol_ratio_slope']['counted'])
        self.assertFalse(payload['turnover_pct']['counted'])
        self.assertEqual(payload['vol_ratio_slope']['role'], 'strength')
        self.assertEqual(payload['vol_compress']['group'], 'price')
        # 得分只算计票代表：(60×1 + 30×1)/2
        self.assertEqual(data['score'], 45.0)
        self.assertEqual(data['strength'], round(45.0 * 2.0, 2))

    def test_down_signal_from_volume_shrink_no_longer_fires(self):
        configs = [_cfg('vol_ratio_slope', 'down'), _cfg('turnover_pct', 'down')]
        data, _dims = signals.evaluate_target(self._ctx(), configs)
        self.assertIsNone(data)


def _series_ctx(n=60, kind='stock'):
    dates = [(_TODAY - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]
    closes = [10.0 + 0.01 * i for i in range(n)]
    return {'kind': kind, 'code': '600000', 'name': 'x', 'dates': dates, 'close': closes,
            'open': list(closes), 'high': [c * 1.01 for c in closes], 'low': [c * 0.99 for c in closes],
            'volume': [100.0] * n, 'as_of': dates[-1],
            'margin': [{'date': d, 'total': 1.0} for d in dates]}


class SliceCtxTests(SimpleTestCase):
    def test_slice_truncates_aligned_series_and_margin(self):
        ctx = _series_ctx()
        sub = signals.slice_ctx(ctx, 49)
        self.assertEqual(len(sub['close']), 50)
        self.assertEqual(sub['as_of'], ctx['dates'][49])
        # 融资余额 T+1 披露：只保留数据日 < 截断日的行，不前视
        self.assertTrue(all(m['date'] < sub['as_of'] for m in sub['margin']))
        self.assertEqual(len(sub['margin']), 49)
        self.assertEqual(len(ctx['close']), 60)  # 原 ctx 不被改写


@patch('stocks.trading_calendar.is_trading_day', _always_trading)
class StateCycleTests(TestCase):
    """#2：周期按「方向组同向状态」回看，不要求此前几天也平静、也落过信号行。"""

    def _configs(self, start_idx):
        """价格组从第 start_idx 根起投 up；量能一直「活跃度抬升」。"""
        def price(ctx, params):
            up = len(ctx['dates']) - 1 >= start_idx
            return {'available': True, 'value': 50.0, 'direction': 'up' if up else None,
                    'note': '', 'series': []}

        cfgs = [_cfg('vol_ratio_slope', 'up')]
        cfgs.append({'key': 'vol_compress', 'name': 'vc', 'weight': 1.0, 'params': {},
                     'targets': ('stock',), 'evaluate': price, 'enabled': True, 'defaults': {}})
        return cfgs

    def test_cycles_from_dimension_state_without_signal_rows(self):
        ctx = _series_ctx()
        data, _dims = signals.evaluate_target(ctx, self._configs(56))  # 56..59 共 4 天同向
        self.assertEqual(data['cycles'], 4)
        self.assertEqual(data['status'], DetectorSignal.STATUS_ACTIVE)
        self.assertFalse(DetectorSignal.objects.exists())

    def test_first_day_of_state_is_watching(self):
        data, _dims = signals.evaluate_target(_series_ctx(), self._configs(59))
        self.assertEqual(data['cycles'], 1)
        self.assertEqual(data['status'], DetectorSignal.STATUS_WATCHING)

    def test_signal_rows_still_count_when_ctx_has_no_series(self):
        # 无序列可截断（如测试桩 ctx）时退回信号行计数，行为与旧实现一致
        DetectorSignal.objects.create(target_kind='stock', target_code='600000', target_name='x',
                                      trade_date=_TODAY - timedelta(days=1), direction='up')
        ctx = {'kind': 'stock', 'code': '600000', 'name': 'x', 'as_of': _TODAY.isoformat()}
        data, _ = signals.evaluate_target(ctx, [_cfg('a', 'up'), _cfg('b', 'up')])
        self.assertEqual(data['cycles'], 2)

    def test_gap_in_series_breaks_state_streak(self):
        ctx = _series_ctx()
        # 去掉 T−2 那根（停牌/缺快照）：回看到 T−2 即断链
        for key in ('dates', 'close', 'open', 'high', 'low', 'volume'):
            del ctx[key][-3]
        data, _ = signals.evaluate_target(ctx, self._configs(0))
        self.assertEqual(data['cycles'], 2)


def _random_walk_ctx(rng, n=70, sigma=0.015):
    p, closes, highs, lows, vols = 10.0, [], [], [], []
    for _ in range(n):
        pc = p
        p *= 1 + rng.gauss(0, sigma)
        closes.append(p)
        amp = abs(rng.gauss(0, 0.012)) + 0.01
        highs.append(max(p, pc) * (1 + amp / 2))
        lows.append(min(p, pc) * (1 - amp / 2))
        vols.append(1e6 * math.exp(rng.gauss(0, 0.3)))
    dates = [str(i) for i in range(n)]
    return {'kind': 'stock', 'code': '000001', 'name': 'x', 'dates': dates, 'open': list(closes),
            'close': closes, 'high': highs, 'low': lows, 'volume': vols, 'as_of': dates[-1]}


class GroupedNoiseRegressionTests(SimpleTestCase):
    """regime 化平静闸门 + 分组投票在随机游走下的噪声信号率仍 < 5%（ALG-04 口径）。"""

    def _configs(self, kind):
        return [
            {'key': s['key'], 'name': s['name'], 'weight': 1.0, 'params': dict(s['defaults']),
             'targets': s['targets'], 'evaluate': s['evaluate'], 'enabled': True, 'defaults': s['defaults'],
             'group': s['group'], 'role': s['role']}
            for s in registry.DIMENSIONS if s['enabled'] and kind in s['targets']
        ]

    def test_stock_noise_rate_given_calm(self):
        rng = random.Random(7)
        configs = self._configs('stock')
        calm_n = fired = 0
        for _ in range(500):
            ctx = _random_walk_ctx(rng)
            ok, _meta = calm.is_calm(ctx)
            if not ok:
                continue
            calm_n += 1
            fired += signals.vote(signals._evaluate_dims(ctx, configs))['direction'] is not None
        self.assertGreater(calm_n, 50)
        self.assertLess(fired / calm_n, 0.05)

    def test_sector_noise_rate_given_calm(self):
        rng = random.Random(7)
        configs = self._configs('industry')
        calm_n = fired = 0
        for _ in range(1200):
            n = 45
            dates = [str(i) for i in range(n)]
            ctx = {'kind': 'industry', 'code': 'x', 'name': 'x', 'dates': dates, 'as_of': dates[-1],
                   'net': [rng.gauss(0, 3e8) for _ in range(n)],
                   'change_pct': [rng.gauss(0, 1.2) for _ in range(n)]}
            ok, _meta = calm.is_calm(ctx)
            if not ok:
                continue
            calm_n += 1
            fired += signals.vote(signals._evaluate_dims(ctx, configs))['direction'] is not None
        self.assertGreater(calm_n, 100)
        self.assertLess(fired / calm_n, 0.05)

    def test_calm_regime_persists_more_than_single_day_gate(self):
        # 旧闸门次日保持率约 30%（01_code_audit §3）；迟滞后应明显更高
        rng = random.Random(3)
        both = first = 0
        for _ in range(250):
            ctx = _random_walk_ctx(rng, n=71)
            prev = {k: (v[:-1] if isinstance(v, list) else v) for k, v in ctx.items()}
            prev['as_of'] = prev['dates'][-1]
            ok_prev, _ = calm.is_calm(prev)
            if not ok_prev:
                continue
            first += 1
            both += calm.is_calm(ctx)[0]
        self.assertGreater(first, 20)
        self.assertGreater(both / first, 0.45)


class MultipleTestingTests(SimpleTestCase):
    """#11：期望假阳数与按强度截断。"""

    def _configs(self):
        return [
            {'key': s['key'], 'name': s['name'], 'weight': 1.0, 'params': dict(s['defaults']),
             'targets': s['targets'], 'evaluate': s['evaluate'], 'enabled': s['enabled'],
             'defaults': s['defaults'], 'group': s.get('group', s['key']), 'role': s.get('role', 'direction')}
            for s in registry.DIMENSIONS
        ]

    def test_null_fire_probability_is_small_and_kind_specific(self):
        configs = self._configs()
        p_stock = scan.null_fire_probability(configs, 'stock')
        p_sector = scan.null_fire_probability(configs, 'industry')
        p_hs300 = scan.null_fire_probability(configs, 'index', 'sh000300')
        p_index = scan.null_fire_probability(configs, 'index', 'sh000001')
        for p in (p_stock, p_sector, p_hs300, p_index):
            self.assertGreater(p, 0.0)
            self.assertLess(p, 0.05)
        # 沪深300 多一组融资余额，概率不低于其他指数
        self.assertGreaterEqual(p_hs300, p_index)

    def test_expected_false_positives_scale_with_tested(self):
        configs = self._configs()
        p = scan.null_fire_probability(configs, 'industry')
        many = scan.multiple_testing_stats(configs, [('industry', str(i)) for i in range(500)], 7)
        self.assertAlmostEqual(many['expected_false_positives'], p * 500, delta=0.01)
        self.assertAlmostEqual(many['expected_by_kind']['industry'], p * 500, delta=0.01)
        self.assertEqual(many['tested'], 500)
        self.assertEqual(many['signals'], 7)

    def test_truncate_keeps_strongest_sectors_and_never_cuts_watchlist(self):
        def item(kind, code, strength, cycles=1):
            return (f'{kind}:{code}', {'target_kind': kind, 'target_code': code,
                                       'strength': strength, 'cycles': cycles}, {})

        collected = [item('industry', f's{i}', float(i)) for i in range(6)]
        collected += [item('stock', '600000', 0.1), item('index', 'sh000300', 0.2)]
        kept, dropped = scan.truncate_by_strength(collected, 3)
        kept_codes = {c[1]['target_code'] for c in kept}
        self.assertEqual(kept_codes, {'s5', 's4', 's3', '600000', 'sh000300'})
        self.assertEqual({c[1]['target_code'] for c in dropped}, {'s0', 's1', 's2'})
        kept, dropped = scan.truncate_by_strength(collected, 0)  # 0 = 不截断
        self.assertEqual((len(kept), len(dropped)), (8, 0))


def _after_close_clock():
    from datetime import datetime, time as dt_time
    from types import SimpleNamespace

    fixed = timezone.make_aware(datetime.combine(_TODAY, dt_time(16, 0)))
    return SimpleNamespace(localdate=lambda *a: fixed.date(), localtime=lambda *a: fixed)


@patch('stocks.trading_calendar.is_trading_day', _always_trading)
class ScanTruncationTests(TestCase):
    """run_scan：板块信号超上限按强度截断，截掉的不落库；run.message 带期望假阳数。"""

    def _run(self, n_sectors):
        targets = [{'kind': 'industry', 'code': f'板块{i}', 'name': f'板块{i}'} for i in range(n_sectors)]

        def fake_eval(ctx, configs):
            i = int(ctx['code'].replace('板块', ''))
            data = {'target_kind': 'industry', 'target_code': ctx['code'], 'target_name': ctx['code'],
                    'trade_date': _TODAY.isoformat(), 'direction': 'up', 'cycles': 1, 'score': 40.0 + i,
                    'severity': DetectorSignal.SEVERITY_MEDIUM, 'status': DetectorSignal.STATUS_WATCHING,
                    'cycles_unknown': False, 'strength': (40.0 + i) * 2}
            return data, {}

        with patch.object(scan, 'timezone', _after_close_clock()), \
                patch.object(scan, 'save_margin_snapshot', return_value=0), \
                patch.object(scan, '_collect_targets', return_value=targets), \
                patch.object(scan, '_build_context',
                             side_effect=lambda t: ({'kind': t['kind'], 'code': t['code'],
                                                     'as_of': _TODAY.isoformat()}, None)), \
                patch.object(scan.calm_mod, 'is_calm', return_value=(True, {})), \
                patch.object(scan.signals, 'evaluate_target', side_effect=fake_eval):
            return scan.run_scan()

    @override_settings(DETECTOR_SECTOR_SIGNAL_CAP=2)
    def test_sector_signals_truncated_by_strength(self):
        summary = self._run(5)
        codes = set(DetectorSignal.objects.values_list('target_code', flat=True))
        self.assertEqual(codes, {'板块4', '板块3'})
        self.assertEqual(summary['multiple_testing']['truncated'], 3)
        self.assertEqual(summary['multiple_testing']['tested'], 5)
        run = DetectorRun.objects.get(trade_date=_TODAY)
        self.assertIn('期望假阳', run.message)
        self.assertIn('截掉 3 个', run.message)

    @override_settings(DETECTOR_SECTOR_SIGNAL_CAP=0)
    def test_cap_zero_disables_truncation(self):
        summary = self._run(4)
        self.assertEqual(DetectorSignal.objects.count(), 4)
        self.assertEqual(summary['multiple_testing']['truncated'], 0)
