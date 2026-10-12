"""成分池限速采集队列：间隔、退避、断点续跑、已有数据跳过、增量优先与停牌复查。"""

from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from stocks.models import DailyQuote, IndexConstituent, PoolStockState, Stock
from stocks.pool import collector

T = date(2026, 10, 12)
_DAILY = (collector.MODE_INCREMENT, collector.MODE_BACKFILL)


def _seed_pool(codes):
    for c in codes:
        IndexConstituent.objects.create(index_code='000300', code=c, name=f'N{c}')


def _write_bars(code, days, end=T):
    stock, _ = Stock.objects.get_or_create(code=code, defaults={'name': code, 'is_active': False})
    rows = []
    d = end
    while len(rows) < days:
        if d.weekday() < 5:
            p = Decimal('10')
            rows.append(DailyQuote(stock=stock, trade_date=d, open_price=p, close_price=p, high_price=p,
                                   low_price=p, open_close_diff=0, open_close_pct=0, high_low_diff=0,
                                   high_low_pct=0, volume=100, turnover=Decimal('1000'), is_final=True))
        d -= timedelta(days=1)
    DailyQuote.objects.bulk_create(rows, ignore_conflicts=True)
    return len(rows)


class FakeFetch:
    """替身 fetch_daily_data：按代码返回预设行为（写 N 根 / 抛异常 / 返回 0）。"""

    def __init__(self, behaviour=None, default_bars=260, end=T):
        self.behaviour = behaviour or {}
        self.default_bars = default_bars
        self.end = end
        self.calls = []

    def __call__(self, stock, start_date=None, end_date=None, sources=None, **kw):
        self.calls.append((stock.code, start_date, end_date, [s.name for s in sources or []]))
        b = self.behaviour.get(stock.code, self.default_bars)
        if isinstance(b, Exception):
            raise b
        if callable(b):
            b = b()
        if not b:
            return 0
        return _write_bars(stock.code, b, end=self.end)


@override_settings(POOL_FETCH_ENABLED=True, POOL_FETCH_INTERVAL_SECONDS=20, POOL_MIN_BARS=250,
                   POOL_DAILY_SOURCES=['sina', 'eastmoney', 'baostock', 'tencent'])
class CollectorTests(TestCase):
    def setUp(self):
        collector.reset_queue_state()
        self.addCleanup(collector.reset_queue_state)
        p = mock.patch('stocks.trading_calendar.expected_latest_date', return_value=T)
        p.start()
        self.addCleanup(p.stop)

    def _patch_fetch(self, fake):
        p = mock.patch('stocks.ingest.fetch_daily_data', fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def test_watchlist_stocks_excluded(self):
        _seed_pool(['600000', '600001'])
        Stock.objects.create(code='600000', name='浦发', is_active=True)
        self.assertEqual(collector.pool_codes(), ['600001'])

    def test_backfill_uses_window_and_source_order(self):
        _seed_pool(['600001'])
        fake = self._patch_fetch(FakeFetch())
        res = collector.tick()
        self.assertEqual(res['status'], 'ok')
        self.assertEqual(res['mode'], 'backfill')
        code, start, end, sources = fake.calls[0]
        self.assertEqual(end, '20261012')
        self.assertEqual(sources[0], 'sina')  # 新浪在前（境外东财不可达、腾讯无成交额）
        st = PoolStockState.objects.get(code='600001')
        self.assertIsNotNone(st.backfilled_at)
        self.assertEqual(st.bar_count, 260)
        self.assertEqual(st.last_bar_date, T)
        # 新建的承载行不进自选
        self.assertFalse(Stock.objects.get(code='600001').is_active)

    def test_existing_data_skips_backfill_without_request(self):
        _seed_pool(['600001'])
        _write_bars('600001', 255)
        fake = self._patch_fetch(FakeFetch())
        res = collector.tick(modes=_DAILY)
        # 已有 255 根 ≥ 250：直接标记回补完成；最新日已是 T，也无需增量
        self.assertEqual(res['status'], 'idle')
        self.assertEqual(fake.calls, [])
        self.assertIsNotNone(PoolStockState.objects.get(code='600001').backfilled_at)

    def test_increment_has_priority_over_backfill(self):
        _seed_pool(['600001', '600002'])
        _write_bars('600001', 255, end=T - timedelta(days=3))  # 已回补但缺 T
        fake = self._patch_fetch(FakeFetch())
        collector.sync_states()
        PoolStockState.objects.filter(code='600001').update(backfilled_at=timezone.now())
        res = collector.tick()
        self.assertEqual((res['code'], res['mode']), ('600001', 'increment'))
        self.assertIsNone(fake.calls[0][1])  # 增量不传起点，由入库层从最新 bar 重叠续拉
        self.assertEqual(PoolStockState.objects.get(code='600001').checked_date, T)
        res = collector.tick()
        self.assertEqual((res['code'], res['mode']), ('600002', 'backfill'))

    def test_failure_backs_off_stock_and_queue(self):
        _seed_pool(['600001', '600002', '600003', '600004'])
        fake = self._patch_fetch(FakeFetch({c: ConnectionError('proxy') for c in
                                            ('600001', '600002', '600003', '600004')}))
        r1 = collector.tick()
        self.assertEqual(r1['status'], 'error')
        self.assertEqual(r1['retry_in'], collector.STOCK_BACKOFF_BASE)
        st = PoolStockState.objects.get(code='600001')
        self.assertEqual(st.fail_count, 1)
        self.assertIn('proxy', st.last_error)
        # 退避中的股票不挡队列：下一次轮到 600002
        self.assertEqual(collector.tick()['code'], '600002')
        collector.tick()
        # 连续 3 次失败：队列整体暂停
        self.assertGreater(collector.queue_paused(), 0)
        self.assertEqual(collector.tick()['status'], 'paused')
        self.assertEqual(len(fake.calls), 3)

    def test_zero_rows_counts_as_failure(self):
        _seed_pool(['600001'])
        self._patch_fetch(FakeFetch({'600001': 0}))
        res = collector.tick()
        self.assertEqual(res['status'], 'error')
        self.assertIsNone(PoolStockState.objects.get(code='600001').backfilled_at)

    def test_stock_backoff_doubles(self):
        _seed_pool(['600001'])
        self._patch_fetch(FakeFetch({'600001': ConnectionError('x')}))
        later = timezone.now()
        delays = []
        for _ in range(3):
            collector.reset_queue_state()
            later += timedelta(hours=7)
            delays.append(collector.tick(now=later)['retry_in'])
        self.assertEqual(delays, [300, 600, 1200])

    def test_suspended_stock_rechecked_then_given_up(self):
        _seed_pool(['600001'])
        _write_bars('600001', 255, end=T - timedelta(days=3))
        collector.sync_states()
        PoolStockState.objects.update(backfilled_at=timezone.now())
        # 上游正常返回，但没有 T 这根（停牌）：写入旧日期
        self._patch_fetch(FakeFetch(default_bars=5, end=T - timedelta(days=3)))
        now = timezone.now()
        for i in range(3):
            res = collector.tick(now=now + timedelta(hours=2 * i))
            self.assertEqual(res['mode'], 'increment')
        self.assertTrue(res.get('gave_up'))
        st = PoolStockState.objects.get(code='600001')
        self.assertEqual(st.checked_date, T)
        # 放弃这一天后不再为 T 请求
        self.assertEqual(collector.tick(now=now + timedelta(hours=8), modes=_DAILY)['status'], 'idle')

    def test_run_loop_respects_interval_and_resumes(self):
        _seed_pool([f'60000{i}' for i in range(5)])
        fake = self._patch_fetch(FakeFetch())
        sleeps = []
        clock = iter([0.0, 4.0, 20.0, 23.0, 40.0, 41.0] + [100.0] * 20)
        first = collector.run_loop(limit=2, interval=20, sleep=sleeps.append, clock=lambda: next(clock))
        self.assertEqual([r['code'] for r in first], ['600000', '600001'])
        # 间隔按请求开始时刻计：第一只耗时 4 秒 → 再等 16 秒
        self.assertEqual(sleeps, [16.0])
        # 「中断」后重跑：从断点继续，已回补的不再请求
        rest = collector.run_loop(interval=0, sleep=lambda s: None)
        self.assertEqual([r['code'] for r in rest], ['600002', '600003', '600004'])
        self.assertEqual(len(fake.calls), 5)
        self.assertEqual(PoolStockState.objects.filter(backfilled_at__isnull=False).count(), 5)

    def test_run_loop_sleeps_through_queue_pause(self):
        _seed_pool(['600001'])
        self._patch_fetch(FakeFetch())
        collector._queue_state['pause_until'] = 10 ** 12
        slept = []

        def fake_sleep(sec):
            slept.append(sec)
            collector.reset_queue_state()

        collector.run_loop(interval=0, sleep=fake_sleep)
        self.assertEqual(len(slept), 1)
        self.assertTrue(PoolStockState.objects.get(code='600001').backfilled_at)

    @override_settings(POOL_FETCH_ENABLED=False)
    def test_disabled(self):
        self.assertEqual(collector.tick()['status'], 'disabled')

    def test_coverage(self):
        _seed_pool(['600001', '600002', '600003'])
        _write_bars('600001', 255)
        _write_bars('600002', 255, end=T - timedelta(days=1))
        cov = collector.coverage(T)
        self.assertEqual((cov['fresh'], cov['expected']), (1, 2))
        self.assertEqual(cov['missing'], ['600002'])
        self.assertEqual(cov['not_ready'], ['600003'])
        self.assertAlmostEqual(cov['ratio'], 0.5)

    def test_float_shares_refresh_cached(self):
        _seed_pool(['600001'])
        _write_bars('600001', 255)
        self._patch_fetch(FakeFetch())
        with mock.patch('stocks.datasources.float_shares.fetch_float_share_history',
                        return_value=[(date(2020, 1, 1), 1e9)]):
            res = collector.tick()
        self.assertEqual(res['mode'], 'float_shares')
        st = PoolStockState.objects.get(code='600001')
        self.assertEqual(st.float_shares, [['2020-01-01', 1e9]])


@override_settings(POOL_FETCH_ENABLED=True, POOL_MIN_BARS=250)
class BackfillCommandTests(TestCase):
    def setUp(self):
        collector.reset_queue_state()
        p = mock.patch('stocks.trading_calendar.expected_latest_date', return_value=T)
        p.start()
        self.addCleanup(p.stop)

    def test_status_and_backfill(self):
        _seed_pool(['600001', '600002'])
        out = StringIO()
        call_command('backfill_pool_quotes', '--status', stdout=out)
        self.assertIn('backfill_remaining: 2', out.getvalue())
        with mock.patch('stocks.ingest.fetch_daily_data', FakeFetch()), \
                mock.patch('stocks.pool.collector.time.sleep'):
            out = StringIO()
            call_command('backfill_pool_quotes', '--interval', '5', stdout=out, stderr=StringIO())
        self.assertIn('已回补 2/2', out.getvalue())

    def test_empty_pool_errors(self):
        from django.core.management import CommandError
        with self.assertRaises(CommandError):
            call_command('backfill_pool_quotes', stdout=StringIO())


class SchedulerJobTests(TestCase):
    def test_job_skips_when_lock_busy(self):
        from stocks import scheduler as sch
        from stocks.task_lock import TaskLock

        lock = TaskLock('pool_quotes')
        self.assertTrue(lock.acquire(timeout=0))
        try:
            with mock.patch('stocks.pool.collector.tick') as tick:
                self.assertIsNone(sch._pool_quotes_job())
            tick.assert_not_called()
        finally:
            lock.release()

    def test_job_registered_with_interval(self):
        from stocks import scheduler as sch

        job = {j.id: j for j in sch.build_scheduler().get_jobs()}['pool_quotes']
        self.assertEqual(job.max_instances, 1)
        self.assertEqual(job.trigger.interval.total_seconds(), collector.interval_seconds())
