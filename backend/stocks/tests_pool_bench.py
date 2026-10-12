"""成分池扫描的耗时与峰值内存基准（默认跳过；STOCKTRACE_BENCH=1 时运行）。

VACUUM INTO 不能在事务里执行，所以用 TransactionTestCase。

用法（本地/VPS 均可，只用测试库，不触网）：
    STOCKTRACE_BENCH=1 STOCKTRACE_BENCH_N=800 DJANGO_DEBUG=true \\
        python manage.py test stocks.tests_pool_bench

合成 N 只成分股 × 260 根已收盘日线（随机游走）+ 40 日两融明细 + 流通股本缓存，
跑一次真实的 run_scan(pool='force')（真实序列构建、平静判定、维度评估与截面分位，
只断开指数/两融快照上游），记录：
- 扫描墙钟耗时；
- tracemalloc 峰值（扫描期间 Python 分配的峰值，按 DETECTOR_POOL_CHUNK 分块与不分块各测一次）；
- 日线行占用的 SQLite 体积（VACUUM INTO 落盘前后对比，按行折算）。
结果打印到 stdout，并在设置 STOCKTRACE_BENCH_OUT 时写成 JSON。
"""

import json
import os
import random
import time
import tracemalloc
import unittest
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test import TransactionTestCase, override_settings

from .detector import scan
from .models import DailyQuote, IndexConstituent, PoolStockState, Stock, StockMarginDaily
from .tests_detector import _TODAY
from .tests_p1_detector import _scan_patches

BENCH = os.environ.get('STOCKTRACE_BENCH') == '1'
N = int(os.environ.get('STOCKTRACE_BENCH_N', '800'))
# time=不开 tracemalloc 计时；trace=分块(100)测峰值；traceall=不分块测峰值（对照）
MODES = [m.strip() for m in os.environ.get('STOCKTRACE_BENCH_MODES', 'time,trace,traceall').split(',') if m.strip()]
BARS = 260


def _trading_days(n):
    out, d = [], _TODAY
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def _proc_kb(key):
    try:
        with open('/proc/self/status', encoding='ascii') as fh:
            for line in fh:
                if line.startswith(key + ':'):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


def _vacuum_size(path):
    if os.path.exists(path):
        os.remove(path)
    with connection.cursor() as cur:
        cur.execute(f"VACUUM INTO '{path}'")
    size = os.path.getsize(path)
    os.remove(path)
    return size


@unittest.skipUnless(BENCH, '基准测试：设置 STOCKTRACE_BENCH=1 运行')
class PoolScanBenchmark(TransactionTestCase):
    def _seed(self):
        rng = random.Random(7)
        days = _trading_days(BARS)
        codes = [f'{600000 + i:06d}' if i % 2 else f'{1 + i:06d}' for i in range(N)]
        stocks = [Stock(code=c, name=f'B{c}', is_active=False) for c in codes]
        Stock.objects.bulk_create(stocks)
        stocks = {s.code: s for s in Stock.objects.filter(code__in=codes)}
        IndexConstituent.objects.bulk_create(
            [IndexConstituent(index_code='000905', code=c, name=f'B{c}') for c in codes])
        PoolStockState.objects.bulk_create([
            PoolStockState(code=c, bar_count=BARS, last_bar_date=_TODAY, backfilled_at=None,
                           float_shares=[['2015-01-05', 1e9]]) for c in codes])
        rows, margins = [], []
        for c in codes:
            price = 10 + rng.random() * 40
            vol_base = rng.uniform(5e6, 5e7)
            prev = None
            for k, d in enumerate(days):
                price *= 1 + rng.gauss(0, 0.012)
                o = price * (1 + rng.gauss(0, 0.004))
                h = max(o, price) * (1 + abs(rng.gauss(0, 0.004)))
                low = min(o, price) * (1 - abs(rng.gauss(0, 0.004)))
                vol = int(vol_base * (1 + rng.gauss(0, 0.15)))
                q = lambda x: Decimal(f'{x:.3f}')  # noqa: E731
                rows.append(DailyQuote(
                    stock=stocks[c], trade_date=d, open_price=q(o), close_price=q(price), high_price=q(h),
                    low_price=q(low), open_close_diff=q(price - o), open_close_pct=Decimal('0'),
                    high_low_diff=q(h - low), high_low_pct=Decimal('0'),
                    prev_close=q(prev) if prev else None, volume=vol,
                    turnover=Decimal(f'{vol * price:.2f}'), source='sina', is_final=True))
                prev = price
                if k >= BARS - 41 and d < _TODAY:
                    margins.append(StockMarginDaily(
                        code=c, trade_date=d, exchange='sse' if c.startswith('6') else 'szse',
                        rz=int(vol_base * price * rng.uniform(2, 8)), rz_buy=int(vol_base * price * rng.uniform(0.05, 0.3))))
            if len(rows) >= 50000:
                DailyQuote.objects.bulk_create(rows, batch_size=2000)
                rows = []
        DailyQuote.objects.bulk_create(rows, batch_size=2000)
        StockMarginDaily.objects.bulk_create(margins, batch_size=2000)
        return codes

    def _scan(self, chunk, trace):
        with override_settings(DETECTOR_POOL_CHUNK=chunk, DETECTOR_POOL_SIGNAL_CAP=20), \
                _scan_patches([], None, calm_result=None), \
                unittest.mock.patch('stocks.detector.review._benchmark_bars', lambda cache: {}):
            rss0 = _proc_kb('VmRSS')
            if trace:
                tracemalloc.start()
            t0 = time.perf_counter()
            summary = scan.run_scan(pool='force')
            elapsed = time.perf_counter() - t0
            peak = None
            if trace:
                _cur, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
            hwm = _proc_kb('VmHWM')
        return summary, elapsed, peak, (hwm - rss0) if hwm and rss0 else None

    def test_benchmark(self):
        t0 = time.perf_counter()
        empty = _vacuum_size('/tmp/stocktrace_bench_empty.db')
        self._seed()
        seed_s = time.perf_counter() - t0
        full = _vacuum_size('/tmp/stocktrace_bench_full.db')
        n_rows = DailyQuote.objects.count()
        n_margin = StockMarginDaily.objects.count()
        result = {
            'stocks': N, 'bars_per_stock': BARS, 'daily_rows': n_rows, 'margin_rows': n_margin,
            'seed_seconds': round(seed_s, 1),
            'db_bytes_added': full - empty,
            'db_bytes_per_daily_row_upper_bound': round((full - empty) / n_rows, 1),
        }
        summary = None
        for mode in MODES:
            chunk = 10 ** 6 if mode == 'traceall' else 100
            summary, elapsed, peak, rss_delta = self._scan(chunk, trace=mode != 'time')
            result[f'{mode}_scan_seconds'] = round(elapsed, 1)
            if peak is not None:
                result[f'{mode}_tracemalloc_peak_mb'] = round(peak / 2 ** 20, 1)
            if rss_delta is not None:
                result[f'{mode}_rss_hwm_growth_mb'] = round(rss_delta / 1024, 1)
            result['pool_summary'] = summary.get('pool')
            result['calm'] = summary.get('calm_count')
        print('\nPOOL_BENCH ' + json.dumps(result, ensure_ascii=False))
        out = os.environ.get('STOCKTRACE_BENCH_OUT')
        if out:
            with open(out, 'w', encoding='utf-8') as fh:
                json.dump(result, fh, ensure_ascii=False, indent=1)
        self.assertEqual(summary['pool']['status'], 'done')
        self.assertEqual(summary['pool']['scanned'], N)
