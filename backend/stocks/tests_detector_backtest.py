"""探测器离线回测（stocks.detector.backtest + detector_backtest 命令）的回归测试。

全部用合成数据，绝不联网：交易日历用 ListCalendar 注入，数据缓存写进临时目录，
联网取数实现用会直接失败的假 fetchers 顶替。
"""

import csv
import json
import os
import shutil
import tempfile
from datetime import date, timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from .detector import review, signals
from .detector.backtest import context, data, engine, metrics, synthetic
from .detector.backtest.tradedays import ListCalendar


def _weekdays(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _no_global_calendar(*_a, **_k):
    raise AssertionError('注入日历后不应再查全局交易日历')


class _ExplodingFetchers:
    """任何联网调用都直接失败：保证测试不会触网。"""

    def __getattr__(self, name):
        def boom(*_a, **_k):
            raise AssertionError(f'测试中不应联网调用 {name}')
        return boom


class ListCalendarTests(SimpleTestCase):
    def test_list_calendar_is_tristate(self):
        days = _weekdays(date(2024, 1, 1), 60)
        cal = ListCalendar(days)
        self.assertTrue(cal.is_trading_day(days[3]))
        self.assertFalse(cal.is_trading_day(date(2024, 1, 6)))  # 周六
        self.assertIsNone(cal.is_trading_day(date(2023, 12, 1)))  # 覆盖范围外：未知
        self.assertIsNone(cal.is_trading_day(None))
        self.assertEqual(cal.next_days(days[0], 2), days[1:3])
        self.assertIsNone(cal.next_days(days[-1], 1))
        self.assertEqual(cal.prev_days(days[3], 2), [days[2], days[1]])
        self.assertEqual(cal.index_of(days[5].isoformat()), 5)
        with self.assertRaises(ValueError):
            ListCalendar([])

    def test_injected_into_repo_functions(self):
        days = _weekdays(date(2024, 1, 1), 60)
        cal = ListCalendar(days)
        with patch('stocks.trading_calendar.is_trading_day', side_effect=_no_global_calendar):
            self.assertEqual(signals._previous_trading_days(days[10], 5, calendar=cal), days[5:10][::-1])
            self.assertEqual(review._next_trading_days(days[0], 3, calendar=cal), days[1:4])


# ============================================================
# 防前视与引擎
# ============================================================

_OUTCOME_KEYS = {'hit_up', 'hit_down', 'base_up', 'base_down', 'thr'} | {
    f'{p}{n}' for p in ('r', 'b') for n in engine.FWD_NS}


def _strip_outcomes(rows):
    """去掉评估用的前瞻字段（它们本来就允许看未来），只比较按时点可见的部分。"""
    return [{k: v for k, v in r.items() if k not in _OUTCOME_KEYS} for r in rows]


class NoLookaheadTests(SimpleTestCase):
    def setUp(self):
        self.items = synthetic.make_series(220, seed=11)
        self.cal = ListCalendar([it['date'] for it in self.items])
        self.configs = engine.build_configs()

    def test_build_ctx_equals_truncated_series(self):
        for i in (40, 120, 219):
            full = context.build_ctx('stock', 'x', 'x', self.items, i)
            trunc = context.build_ctx('stock', 'x', 'x', self.items[:i + 1], i)
            self.assertEqual(full, trunc)
            self.assertEqual(full['as_of'], self.items[i]['date'])
            self.assertLessEqual(len(full['dates']), context.DEFAULT_CTX_BARS)

    def test_run_target_rows_unchanged_when_future_removed(self):
        cut = 160
        kw = dict(calendar=self.cal, bench_by_date={}, configs=self.configs, start=self.items[0]['date'], end=None)
        full = engine.run_target({'kind': 'stock', 'code': 'x'}, self.items, **kw)
        trunc = engine.run_target({'kind': 'stock', 'code': 'x'}, self.items[:cut + 1], **kw)
        t = self.items[cut]['date']
        a = _strip_outcomes([r for r in full if r['date'] <= t])
        b = _strip_outcomes(trunc)
        self.assertEqual(a, b)
        self.assertTrue(any(r['calm'] for r in a))

    def test_future_shock_does_not_change_past_dimensions(self):
        shocked = [dict(it) for it in self.items]
        for it in shocked[170:]:
            it['close'] *= 3
            it['volume'] *= 10
        kw = dict(calendar=self.cal, bench_by_date={}, configs=self.configs, start=self.items[0]['date'], end=None,
                  with_outcomes=False)
        a = engine.run_target({'kind': 'stock', 'code': 'x'}, self.items, **kw)
        b = engine.run_target({'kind': 'stock', 'code': 'x'}, shocked, **kw)
        t = self.items[169]['date']
        self.assertEqual([r for r in a if r['date'] <= t], [r for r in b if r['date'] <= t])

    def test_margin_window_only_rows_before_t_and_drops_stale(self):
        # 与生产 load_margin_series 同口径（#8/#9）：交易日轴、末点 T−1、缺失日 None
        dates = [d.isoformat() for d in _weekdays(date(2024, 1, 1), 160)]
        cal = ListCalendar(dates)
        rows = [(d, 100.0 + k) for k, d in enumerate(dates)]
        mdates = [r[0] for r in rows]
        t = 150
        win = context.margin_window(rows, mdates, dates[t], cal)
        self.assertEqual(win[-1]['date'], dates[t - 1])  # T+1 披露：T 当天的数不可见
        self.assertEqual(win[-1]['rz'], rows[t - 1][1])  # 缓存只有 total，代理 rz
        self.assertEqual(len(win), context.MARGIN_CONTEXT_DAYS)
        self.assertTrue(all(m['date'] < dates[t] for m in win))
        # 缺口：日期轴保留缺失日为 None，不压缩时间
        gapped = rows[:100] + rows[105:]
        win = context.margin_window(gapped, [r[0] for r in gapped], dates[t], cal)
        by_date = {m['date']: m['rz'] for m in win}
        self.assertIsNone(by_date[dates[102]])
        self.assertEqual(by_date[dates[106]], rows[106][1])
        # 最新一行落后于 T−1：末点为 None，维度如实不可用
        stale = rows[:140]
        win = context.margin_window(stale, [r[0] for r in stale], dates[t], cal)
        self.assertEqual(win[-1]['date'], dates[t - 1])
        self.assertIsNone(win[-1]['rz'])
        # 轴起点不早于缓存最早一行
        late = rows[30:]
        win = context.margin_window(late, [r[0] for r in late], dates[60], cal)
        self.assertEqual(win[0]['date'], dates[30])

    def test_build_ctx_matches_production_context_fields(self):
        # 集成：上下文长度与生产 CONTEXT_BARS 一致；个股带 volume_adj（#15），指数为原始量
        from .detector.constants import CONTEXT_BARS

        self.assertEqual(context.DEFAULT_CTX_BARS, CONTEXT_BARS)
        stock = context.build_ctx('stock', 'x', 'x', self.items, 219)
        self.assertEqual(len(stock['volume_adj']), len(stock['dates']))
        self.assertIn('volume_basis', stock)
        index = context.build_ctx('index', 'sh000001', 'x', self.items, 219)
        self.assertEqual(index['volume_adj'], index['volume'])

    def test_memory_cycle_counter_counts_consecutive_trading_days(self):
        days = _weekdays(date(2024, 1, 1), 60)
        cal = ListCalendar(days)
        c = engine.MemoryCycleCounter(cal)
        for d in days[35:40]:
            c.record('stock', 'x', d, 'up')
        c.record('stock', 'x', days[33], 'up')  # 中间 days[34] 断开
        self.assertEqual(c('stock', 'x', 'up', days[40]), 5)
        self.assertEqual(c('stock', 'x', 'down', days[40]), 0)
        self.assertEqual(c('stock', 'y', 'up', days[40]), 0)
        self.assertIsNone(c('stock', 'x', 'up', days[3]))  # 日历起点前未知

    def test_outcomes_forward_returns_relative_to_bench(self):
        items = [{'date': d.isoformat(), 'open': 10.0 + k, 'high': 0, 'low': 0, 'close': 10.0 + k, 'volume': 1}
                 for k, d in enumerate(_weekdays(date(2024, 1, 1), 120))]
        cal = ListCalendar([it['date'] for it in items])
        bench = {it['date']: {'open': 100.0, 'close': 100.0} for it in items}
        out = engine.outcomes(items, 100, cal, bench)
        self.assertAlmostEqual(out['r5'], (115.0 / 111.0 - 1) * 100)
        self.assertEqual(out['b5'], 0.0)
        self.assertEqual(out['hit_up'], 1)
        # 停牌缺口：T+1..T+N 不是日历上连续的交易日 → 不计
        gapped = items[:102] + items[103:]
        self.assertIsNone(engine.outcomes(gapped, 100, cal, bench)['r5'])

    def test_bench_regimes_use_trailing_window_only(self):
        items = [{'date': f'd{k:03d}', 'close': 100.0 * (1.002 ** k)} for k in range(100)]
        reg = engine.bench_regimes(items, window=60, band=5.0)
        self.assertNotIn('d059', reg)
        self.assertEqual(reg['d060'], 'up')  # 1.002^60 ≈ +12.7%


class ConfigAndUniverseTests(SimpleTestCase):
    def test_build_configs_override_and_validation(self):
        cfgs = {c['key']: c for c in engine.build_configs({'vol_compress': {'drift_z': 1.5, 'weight': 2}})}
        self.assertEqual(cfgs['vol_compress']['params']['drift_z'], 1.5)
        self.assertEqual(cfgs['vol_compress']['weight'], 2.0)
        self.assertEqual(cfgs['ma_relation']['params']['ma'], 20)  # 未覆盖的保持默认
        with self.assertRaises(ValueError):
            engine.build_configs({'nope': {}})
        with self.assertRaises(ValueError):
            engine.build_configs({'order_imbalance': {'enabled': True}})
        with self.assertRaises(ValueError):
            engine.build_configs({'vol_compress': 3})
        off = {c['key']: c for c in engine.build_configs({'vol_compress': {'enabled': False}})}
        self.assertNotIn('vol_compress', [c['key'] for c in engine.active_dims(off.values())])

    def test_parse_code(self):
        self.assertEqual(engine.parse_code('600000'), 'sh600000')
        self.assertEqual(engine.parse_code('000858'), 'sz000858')
        self.assertEqual(engine.parse_code('SH000300'), 'sh000300')
        with self.assertRaises(ValueError):
            engine.parse_code('abc')

    def test_resolve_universe_sampling_is_seeded_and_mixed(self):
        members = [(f'sh60{k:04d}', f'n{k}') for k in range(50)]
        store = SimpleNamespace(hs300_members=lambda d: list(members))
        a = engine.resolve_universe(store, 'hs300-sample,indices,600000', start='2024-01-02', sample=10, seed=1)
        b = engine.resolve_universe(store, 'hs300-sample,indices,600000', start='2024-01-02', sample=10, seed=1)
        c = engine.resolve_universe(store, 'hs300-sample', start='2024-01-02', sample=10, seed=2)
        self.assertEqual(a, b)
        self.assertNotEqual(a[:10], c)
        codes = [t['code'] for t in a]
        self.assertEqual(len(codes), len(set(codes)))  # 去重
        self.assertIn('sh600000', codes)
        self.assertIn(sum(t['kind'] == 'stock' for t in a), (10, 11))
        self.assertEqual(sum(t['kind'] == 'index' for t in a), 5)
        with self.assertRaises(ValueError):
            engine.resolve_universe(store, '', start='2024-01-02')


# ============================================================
# 指标：重采样基线、超额、IC
# ============================================================

def _metric_rows(n_codes=4, n_days=300, seed=0, trig_rule=None):
    rng = np.random.default_rng(seed)
    rows = []
    for c in range(n_codes):
        for t in range(n_days):
            hit_up = int(rng.random() < 0.4)
            row = {'kind': 'stock', 'code': f'c{c}', 'date': f'2024-{t:04d}', 'year': '2024', 'regime': 'flat',
                   'calm': 1, 'calm_reason': '', 'hit_up': hit_up, 'hit_down': int(rng.random() < 0.4),
                   'base_up': 0.4, 'base_down': 0.4, 'sig_dir': '', 'cycles': 0, 'severity': '',
                   'dim_av': 1, 'dim_dir': '', 'dim_val': None, 'dim_note': ''}
            for n in metrics.NS:
                row[f'r{n}'] = float(rng.normal())
                row[f'b{n}'] = 0.0
            if trig_rule and trig_rule(row, rng):
                row['dim_dir'] = 'up'
            rows.append(row)
    return rows


class MetricsTests(SimpleTestCase):
    def test_perfect_triggers_beat_resampled_baseline(self):
        rows = _metric_rows(trig_rule=lambda r, rng: r['hit_up'] == 1 and rng.random() < 0.3)
        s = metrics.summarize(rows, ['dim'], n_resample=500, seed=1)
        c = s['dims']['dim|stock']['calm']
        self.assertEqual(c['hit_rate'], 1.0)
        self.assertAlmostEqual(c['rand_baseline'], 0.4, delta=0.05)
        self.assertLess(c['p_resample'], 0.01)
        self.assertGreater(c['z_resample'], 5)

    def test_random_triggers_not_significant(self):
        rows = _metric_rows(seed=3, trig_rule=lambda r, rng: rng.random() < 0.1)
        s = metrics.summarize(rows, ['dim'], n_resample=500, seed=1)
        c = s['dims']['dim|stock']['calm']
        self.assertGreater(c['p_resample'], 0.01)
        self.assertLess(abs(c['rand_excess']), 0.1)

    def test_forward_excess_signed_by_direction_and_index_uses_raw(self):
        df = pd.DataFrame([
            {'kind': 'stock', 'code': 'a', 'date': '1', 'r5': 3.0, 'b5': 1.0, 'hit_up': 1, 'hit_down': 0,
             'base_up': None, 'base_down': None, 'd': 'down'},
            {'kind': 'index', 'code': 'i', 'date': '2', 'r5': 2.0, 'b5': 5.0, 'hit_up': 1, 'hit_down': 0,
             'base_up': None, 'base_down': None, 'd': 'up'},
        ])
        df = metrics.to_frame(df.to_dict('records'))
        self.assertEqual(list(df['x5']), [2.0, 2.0])
        trig = pd.concat([df] * 2, ignore_index=True)
        m = metrics.trigger_metrics(trig, 'd', metrics.Pools(df), n_resample=0)
        self.assertAlmostEqual(m['fwd5']['mean'], 0.0)  # down 取反 −2，up +2
        self.assertEqual(m['hit_rate'], 0.5)

    def test_seg_heads_drop_consecutive_days(self):
        df = pd.DataFrame({'code': ['a'] * 5 + ['b'], 'ci': [1, 2, 3, 5, 6, 2], 'd': ['up'] * 4 + ['down', 'up']})
        heads = metrics.seg_heads(df, 'd')
        self.assertEqual(sorted(zip(heads['code'], heads['ci'])), [('a', 1), ('a', 5), ('a', 6), ('b', 2)])

    def test_rank_ic(self):
        v = pd.Series(range(50), dtype=float)
        self.assertAlmostEqual(metrics.rank_ic(v, v ** 3)['ic'], 1.0)
        self.assertAlmostEqual(metrics.rank_ic(v, -v)['ic'], -1.0)
        self.assertIsNone(metrics.rank_ic(v[:10], v[:10]))

    def test_summary_groups_and_signal_layer(self):
        rows = _metric_rows(trig_rule=lambda r, rng: rng.random() < 0.05)
        for r in rows[::50]:
            r.update(sig_dir='up', cycles=2, severity='medium')
        s = metrics.summarize(rows, ['dim'], n_resample=0)
        self.assertIn('2024', s['dims']['dim|stock']['by_year'])
        self.assertEqual(s['signals']['all']['active']['n'], len(rows[::50]))
        self.assertEqual(s['meta']['m_tests'], 0)  # n_resample=0 不计检验次数
        json.dumps(s, default=str)


# ============================================================
# 数据缓存与命令
# ============================================================

def _write_bars(path, items, with_amount):
    data.write_bars_csv(str(path), items, with_amount)


class _CacheMixin:
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix='det_bt_')
        self.cache = os.path.join(self.tmp, 'cache')
        bench = synthetic.make_series(260, seed=1)
        for it in bench:
            it['amount'] = None
        _write_bars(os.path.join(self.cache, 'index', 'sh000300.csv'), bench, False)
        _write_bars(os.path.join(self.cache, 'stocks', 'sh600000.csv'), synthetic.make_series(260, seed=2), True)
        self.first_eval = bench[120]['date']

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()


class DataStoreTests(_CacheMixin, SimpleTestCase):
    def test_offline_reads_cache_and_never_fetches(self):
        st = data.DataStore(self.cache, offline=True, fetchers=_ExplodingFetchers())
        self.assertEqual(len(st.index_bars('sh000300')), 260)
        with self.assertRaises(data.CacheMiss):
            st.stock_bars('sz000001')
        self.assertIsNone(st.share_history('sh600000'))  # 股本缺失 → None（生产同语义）
        self.assertEqual(st.margin_rows(), [])
        self.assertIn('stocks/sz000001.csv', st.missing)

    def test_online_fetch_writes_cache_and_manifest(self):
        class Fake:
            @staticmethod
            def stock_bars(code, start, end):
                return synthetic.make_series(40, seed=5)

            @staticmethod
            def margin_rows():
                return [('2024-01-02', 1e12, 8e11, 18000.0), ('2024-01-03', 1e12, 0, 10000.0)]

            @staticmethod
            def share_history(code):
                return [(date(2020, 1, 1), 1e9)]

        st = data.DataStore(self.cache, offline=False, fetchers=Fake())
        self.assertEqual(len(st.stock_bars('sz000001')), 40)
        self.assertEqual(st.margin_rows(), [('2024-01-02', 18000.0)])  # 单边为 0 的坏行剔除
        self.assertEqual(st.share_history('sz000001'), [(date(2020, 1, 1), 1e9)])
        manifest = json.loads(open(os.path.join(self.cache, 'manifest.json'), encoding='utf-8').read())
        self.assertIn('stocks/sz000001.csv', manifest)
        self.assertIn('fetched_at', manifest['margin.csv'])
        # 已缓存：第二次不再联网
        st2 = data.DataStore(self.cache, offline=False, fetchers=_ExplodingFetchers())
        self.assertEqual(len(st2.stock_bars('sz000001')), 40)

    def test_hs300_members_fallback_to_legacy_universe(self):
        with open(os.path.join(self.cache, 'universe.csv'), 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['code', 'name', 'bs_code'])
            w.writerow(['sh600000', '浦发银行', 'sh.600000'])
        st = data.DataStore(self.cache, offline=True)
        self.assertEqual(st.hs300_members('2024-01-02'), [('sh600000', '浦发银行')])
        self.assertTrue(st.notes)


class CommandTests(_CacheMixin, SimpleTestCase):
    def _run(self, *args):
        out = StringIO()
        call_command('detector_backtest', *args, stdout=out)
        return out.getvalue()

    def test_offline_run_writes_reports_and_resume_reuses_rows(self):
        out_dir = os.path.join(self.tmp, 'out')
        args = ['--start', self.first_eval, '--universe', 'sh600000,sh000300,sz000001', '--offline',
                '--cache-dir', self.cache, '--out', out_dir, '--resamples', '20']
        with patch.object(data, 'NetworkFetchers', _ExplodingFetchers):
            self._run(*args)
        rep = json.loads(open(os.path.join(out_dir, 'report.json'), encoding='utf-8').read())
        self.assertEqual(rep['meta']['targets'], 2)
        self.assertEqual([s['code'] for s in rep['run']['skipped']], ['sz000001'])
        self.assertIn('vol_compress|stock', rep['dims'])
        md = open(os.path.join(out_dir, 'report.md'), encoding='utf-8').read()
        self.assertIn('信号层', md)
        self.assertTrue(os.path.exists(os.path.join(out_dir, 'rows', 'sh600000.json.gz')))
        with patch.object(engine, 'run_target', side_effect=AssertionError('应复用行结果')):
            self._run(*args, '--resume')
        # 参数变了指纹不同：拒绝 --resume
        with self.assertRaises(CommandError):
            self._run(*args, '--resume', '--param-override', '{"vol_compress": {"drift_z": 1.5}}')

    def test_argument_validation(self):
        base = ['--cache-dir', self.cache, '--offline', '--out', os.path.join(self.tmp, 'o')]
        cases = [
            ['--start', '2024-13-01'],
            ['--start', '2024-05-01', '--end', '2024-01-01'],
            ['--param-override', '{bad json'],
            ['--param-override', '[1, 2]'],
            ['--param-override', '{"no_such_dim": {}}'],
            ['--param-override', '@/nonexistent/x.json'],
            ['--universe', 'none'],
            ['--universe', 'hs300-sample'],  # 离线无成分表
            ['--sample', '0'],
            ['--ctx-bars', '10'],
        ]
        for extra in cases:
            with self.subTest(extra=extra), self.assertRaises(CommandError):
                self._run(*base, *extra)

    def test_param_override_from_file(self):
        from .management.commands.detector_backtest import parse_override

        p = os.path.join(self.tmp, 'ov.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump({'ma_relation': {'max_dist': 6.0}}, f)
        self.assertEqual(parse_override(f'@{p}'), {'ma_relation': {'max_dist': 6.0}})
        self.assertEqual(parse_override(None), {})

    def test_synthetic_only_run(self):
        out_dir = os.path.join(self.tmp, 'syn')
        with patch.object(synthetic, 'PATTERNS', {'accumulate_up': synthetic.PATTERNS['accumulate_up']}):
            self._run('--universe', 'none', '--synthetic', '2', '--out', out_dir)
        rep = json.loads(open(os.path.join(out_dir, 'report.json'), encoding='utf-8').read())
        r = rep['synthetic']['patterns']['accumulate_up']
        self.assertTrue(0 <= r['planted_signal_rate'] <= 1)
        self.assertIn('植入形态检测力', open(os.path.join(out_dir, 'report.md'), encoding='utf-8').read())
