"""探测器扫描接入成分池：数据不齐的跳过与留痕、截面在成分池里取、推送上限、补跑状态。"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings

from .detector import scan, xsection
from .models import DetectorRun, DetectorSignal, IndexConstituent, Stock
from .pool.constituents import ensure_pool_stocks
from .tests_detector import _TODAY
from .tests_p1_detector import _scan_patches

N_POOL = 210
POOL_CODES = [f'{600000 + i:06d}' for i in range(N_POOL)]


def _ctx(code, i, as_of=_TODAY, n=30):
    dates = [(as_of - timedelta(days=n - 1 - k)).isoformat() for k in range(n)]
    rows = [{'date': dates[-2], 'rz': 1e8, 'rz_buy': 1e6}]
    opens = [10.0] * n
    closes = [10.0 * (1 + 0.0005 * (i % 50)) for _ in range(n)]
    return {'kind': 'stock', 'code': code, 'name': code, 'dates': dates, 'as_of': dates[-1],
            'open': opens, 'close': closes, 'high': closes, 'low': opens,
            'volume': [1e6] * n, 'amount': [1e6 * (i + 1)] * n, 'stock_margin': rows}


def _signal(ctx, severity='low', direction='down', strength=10.0):
    return {
        'target_kind': 'stock', 'target_code': ctx['code'], 'target_name': ctx.get('name') or ctx['code'],
        'trade_date': ctx['as_of'], 'direction': direction, 'cycles': 1, 'score': 30.0,
        'severity': severity, 'status': DetectorSignal.STATUS_WATCHING, 'strength': strength,
    }


class PoolScanTests(TestCase):
    def setUp(self):
        for code in POOL_CODES:
            IndexConstituent.objects.create(index_code='000905', code=code, name=f'池{code}')
        ensure_pool_stocks()
        self.watch = Stock.objects.create(code='000001', name='平安银行', is_active=True)
        self.stale = {POOL_CODES[3], POOL_CODES[4]}
        self.missing = POOL_CODES[-5:]
        self.seen = {}

    def _build_pool(self, stock, float_history=None):
        i = POOL_CODES.index(stock.code)
        as_of = _TODAY - timedelta(days=1) if stock.code in self.stale else _TODAY
        return _ctx(stock.code, i, as_of=as_of), ''

    def _evaluate(self, ctx, configs, **kw):
        self.seen[ctx['code']] = dict(ctx.get('xsec') or {})
        code = ctx['code']
        if code == '000001':
            return _signal(ctx, 'medium', 'down'), {}
        i = POOL_CODES.index(code)
        if i % 10 == 0:
            sev = ('high', 'medium', 'low')[i % 3]
            return _signal(ctx, sev, 'up' if i % 20 == 0 else 'down', strength=i), {}
        return None, {}

    def _coverage(self, ratio_missing=None):
        missing = self.missing if ratio_missing is None else ratio_missing

        def cov(trade_date, codes=None):
            codes = list(codes)
            fresh = [c for c in codes if c not in missing]
            return {'members': len(codes), 'expected': len(codes), 'fresh': len(fresh),
                    'missing': [c for c in codes if c in missing], 'not_ready': [],
                    'ratio': len(fresh) / len(codes)}
        return cov

    def _run(self, mode='auto', missing=None):
        target = {'kind': 'stock', 'code': '000001', 'name': '平安银行', 'stock': self.watch,
                  'source': DetectorSignal.SOURCE_WATCHLIST}
        with _scan_patches([target], lambda t: (_ctx('000001', 999), ''), calm_result=(True, {})), \
                patch.object(scan, 'build_stock_context', side_effect=self._build_pool), \
                patch('stocks.pool.collector.coverage', side_effect=self._coverage(missing)), \
                patch.object(scan.signals, 'evaluate_target', side_effect=self._evaluate):
            return scan.run_scan(pool=mode)

    @override_settings(DETECTOR_POOL_READY_RATIO=0.95, DETECTOR_POOL_SIGNAL_CAP=5)
    def test_pool_scanned_with_skips_recorded_and_cap(self):
        summary = self._run()
        self.assertEqual(summary['pool']['status'], 'done')
        # 210 只 − 5 只当日未入库 − 2 只序列过期
        self.assertEqual(summary['pool']['scanned'], 203)
        self.assertEqual(summary['pool']['skipped'], 2)
        run = DetectorRun.objects.get(trade_date=_TODAY)
        self.assertEqual(run.pool_status, 'done')
        cov = run.pool_coverage
        self.assertEqual(cov['missing_count'], 5)
        self.assertEqual(sorted(cov['missing']), sorted(self.missing))
        self.assertEqual({s['code'] for s in cov['skipped']}, self.stale)
        self.assertIn('数据过期', cov['skipped'][0]['reason'])
        self.assertIn('pool:skipped', {d['target'] for d in run.degraded})
        # 推送上限：成分池 21 条原始信号只留 5 条；自选信号不受影响
        pool_sigs = DetectorSignal.objects.filter(source='pool')
        self.assertEqual(pool_sigs.count(), 5)
        self.assertEqual(cov['signals_raw'], 21)
        self.assertEqual(cov['signals_pushed'], 5)
        self.assertEqual(DetectorSignal.objects.get(target_code='000001').source, 'watchlist')
        # 按严重度取：全部是 high；同为 high 时向下优先
        self.assertEqual(set(pool_sigs.values_list('severity', flat=True)), {'high'})
        self.assertFalse(DetectorSignal.objects.filter(source='pool', target_code__in=self.missing).exists())

    @override_settings(DETECTOR_POOL_SIGNAL_CAP=0)
    def test_dtc_id20_percentile_from_pool(self):
        self._run()
        xs = self.seen['000001']
        self.assertEqual(xs['dtc_basis'], 'pool')
        self.assertEqual(xs['dtc_pool'], 203)
        self.assertIsNotNone(xs['dtc_pct'])
        self.assertEqual(xs['id20_pool'], 203)
        self.assertIsNotNone(xs['id20_pct'])
        # 池内个股也拿到池内分位：成交额最大的那只 DTC 最小
        self.assertAlmostEqual(self.seen[POOL_CODES[0]]['dtc_pct'], 1.0)
        self.assertAlmostEqual(self.seen[POOL_CODES[-6]]['dtc_pct'], 1 / 203)

    @override_settings(DETECTOR_POOL_SIGNAL_CAP=0)
    def test_chunk_size_does_not_change_result(self):
        self._run()
        big = sorted(DetectorSignal.objects.filter(source='pool').values_list('target_code', flat=True))
        pct_big = {k: v.get('dtc_pct') for k, v in self.seen.items()}
        DetectorSignal.objects.all().delete()
        DetectorRun.objects.all().delete()
        self.seen = {}
        with override_settings(DETECTOR_POOL_CHUNK=10):
            self._run()
        small = sorted(DetectorSignal.objects.filter(source='pool').values_list('target_code', flat=True))
        self.assertEqual(big, small)
        self.assertEqual(pct_big, {k: v.get('dtc_pct') for k, v in self.seen.items()})

    @override_settings(DETECTOR_POOL_READY_RATIO=0.95)
    def test_auto_defers_when_incomplete_and_pending_does_not_override_done(self):
        half = POOL_CODES[:100]
        summary = self._run(missing=half)
        self.assertEqual(summary['pool']['status'], 'pending')
        run = DetectorRun.objects.get(trade_date=_TODAY)
        self.assertEqual(run.pool_status, 'pending')
        self.assertIn('未扫成分池', run.message)
        self.assertFalse(DetectorSignal.objects.filter(source='pool').exists())
        # 自选照常出信号，DTC 无成分池参照 → 不给分位
        self.assertTrue(DetectorSignal.objects.filter(source='watchlist').exists())
        self.assertIsNone(self.seen['000001']['dtc_pct'])
        # 数据补齐后补跑：成分池扫描完成
        self._run()
        self.assertEqual(DetectorRun.objects.get(trade_date=_TODAY).pool_status, 'done')
        n_pool = DetectorSignal.objects.filter(source='pool').count()
        self.assertGreater(n_pool, 0)
        # 之后的 auto 重跑若完整度又不足：不把 done 打回 pending，也不清掉成分池信号
        self._run(missing=half)
        self.assertEqual(DetectorRun.objects.get(trade_date=_TODAY).pool_status, 'done')
        self.assertEqual(DetectorSignal.objects.filter(source='pool').count(), n_pool)

    def test_force_scans_whatever_is_fresh(self):
        summary = self._run(mode='force', missing=POOL_CODES[:100])
        self.assertEqual(summary['pool']['status'], 'done')
        # 前 100 只（含 2 只过期的）当日未入库，截止点照扫其余 110 只
        self.assertEqual(summary['pool']['scanned'], 110)

    def test_skip_mode_and_empty_constituents(self):
        summary = self._run(mode='skip')
        self.assertEqual(summary['pool']['status'], 'off')
        IndexConstituent.objects.all().delete()
        plan = scan.plan_pool(_TODAY, set())
        self.assertEqual(plan['status'], 'off')
        self.assertIn('成分表为空', plan['coverage']['reason'])


class PoolReferenceTests(TestCase):
    def test_pct_inside_and_outside_pool(self):
        ref = xsection.PoolReference({'dtc': {'a': 1.0, 'b': 2.0, 'c': 3.0, 'd': 4.0}})
        self.assertEqual(ref.size('dtc'), 4)
        self.assertAlmostEqual(ref.pct('dtc', 'd', 4.0), 1.0)
        self.assertAlmostEqual(ref.pct('dtc', 'a', 1.0), 0.25)
        # 池外：插入后 5 个样本，2.5 排第 3 → 3/5
        self.assertAlmostEqual(ref.pct('dtc', 'x', 2.5), 0.6)
        # 与池内并列取平均秩：插入 2.0 → 秩 2、3 平均 2.5 → 2.5/5
        self.assertAlmostEqual(ref.pct('dtc', 'x', 2.0), 0.5)
        self.assertIsNone(ref.pct('dtc', 'x', None))

    def test_pool_below_min_pool_gives_no_pct(self):
        ctx = _ctx('000001', 3)
        ref = xsection.PoolReference({'dtc': {'a': 1.0}, 'id20': {'a': 0.0}})
        xsection.attach_cross_section([ctx], {'margin_crowding': {'min_pool': 200},
                                              'intraday_reversal': {'min_pool': 200}}, pool_ref=ref)
        self.assertIsNone(ctx['xsec']['dtc_pct'])
        self.assertIn('成分池有效样本 1 只 < 200', ctx['xsec']['dtc_reason'])
        res = xsection.eval_intraday_reversal(ctx, {'min_pool': 200})
        self.assertFalse(res['available'])
        self.assertIn('成分池', res['note'])


class TruncatePoolSignalsTests(TestCase):
    def test_order_severity_then_down_then_strength(self):
        def item(code, sev, d, st, source='pool'):
            return (f'stock:{code}', {'target_kind': 'stock', 'target_code': code, 'severity': sev,
                                      'direction': d, 'strength': st, 'source': source}, {})
        collected = [
            item('a', 'low', 'down', 99), item('b', 'high', 'up', 90), item('c', 'high', 'down', 10),
            item('d', 'medium', 'down', 50), item('w', 'low', 'up', 1, source='watchlist'),
        ]
        kept, dropped = scan.truncate_pool_signals(collected, 2)
        self.assertEqual([k for k, _s, _d in kept], ['stock:b', 'stock:c', 'stock:w'])
        self.assertEqual(sorted(k for k, _s, _d in dropped), ['stock:a', 'stock:d'])
        kept, _ = scan.truncate_pool_signals(collected, 1)
        self.assertIn('stock:c', [k for k, _s, _d in kept])  # 同为 high：向下优先
        self.assertEqual(scan.truncate_pool_signals(collected, 0)[1], [])


class PoolRescanSchedulerTests(TestCase):
    """17:15 成分池日线未齐 → 后续重试点按完整度补跑，截止点强制按现有数据扫。"""

    def _at(self, hh, mm):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime(_TODAY.year, _TODAY.month, _TODAY.day, hh, mm, tzinfo=ZoneInfo('Asia/Shanghai'))

    def _job(self, hh, mm, plan_status):
        from stocks import scheduler

        plan = {'status': plan_status, 'codes': [], 'names': {},
                'coverage': {'fresh': 400, 'expected': 780}}
        with patch('stocks.trading_calendar.is_trading_day', return_value=True), \
                patch('stocks.scheduler.timezone.localtime', return_value=self._at(hh, mm)), \
                patch('stocks.detector.scan.plan_pool', return_value=plan), \
                patch('stocks.detector.scan.run_scan') as run_scan:
            scheduler._detector_scan_job()
        return run_scan

    def test_done_run_is_not_rescanned(self):
        DetectorRun.objects.create(trade_date=_TODAY, status='ok', pool_status='done')
        self.assertFalse(self._job(20, 15, 'done').called)

    def test_pending_waits_then_rescans_when_ready(self):
        DetectorRun.objects.create(trade_date=_TODAY, status='ok', pool_status='pending')
        self.assertFalse(self._job(19, 15, 'pending').called)
        run_scan = self._job(20, 15, 'done')
        run_scan.assert_called_once_with(pool='auto')

    @override_settings(DETECTOR_POOL_DEADLINE='23:45')
    def test_deadline_forces_scan(self):
        DetectorRun.objects.create(trade_date=_TODAY, status='ok', pool_status='pending')
        with self.assertLogs('stocks.scheduler', level='WARNING'):
            run_scan = self._job(23, 45, 'pending')
        run_scan.assert_called_once_with(pool='force')

    def test_bad_deadline_falls_back(self):
        from stocks import scheduler
        from datetime import time as dt_time

        with override_settings(DETECTOR_POOL_DEADLINE='late'), self.assertLogs('stocks.scheduler', 'WARNING'):
            self.assertEqual(scheduler._pool_deadline(), dt_time(23, 45))
