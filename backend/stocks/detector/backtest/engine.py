"""回测引擎：逐标的、逐日回放探测器（平静 → 维度 → 投票/周期 → 复盘判定与前瞻收益）。

直接调用仓库函数，通过正式注入点替换两处依赖：
- signals.evaluate_target(..., cycle_counter=MemoryCycleCounter, calendar=ListCalendar)：
  周期计数走内存信号表，方向组状态回看的交易日序列走本地日历（不触网）；
- review._evaluate_bars(..., calendar=ListCalendar)：复盘窗口校验走本地交易日历。

每行（标的·日）输出：平静结果、各维度 available/direction/value（所有日都评估，
以便做「无平静闸门」对照；生产只在平静日评估）、平静日的信号（方向/周期/得分/
分级）、双向复盘判定与仓库基线、T+1 开盘 → T+N 收盘收益及同窗口沪深300收益。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import random
import re
from pathlib import Path
from types import SimpleNamespace

from ...common.codes import exchange_prefix
from ...market.indices import TREND_INDICES
from .. import calm as calm_mod
from .. import registry, review, signals
from ..constants import (
    BASELINE_DAYS,
    CYCLE_LOOKBACK,
    MIN_PRICE_BARS,
    REVIEW_LOOKBACK,
    SIGMA_WINDOW,
)
from .context import DEFAULT_CTX_BARS, MARGIN_PROXY_NOTE, build_ctx
from .data import CacheMiss
from .tradedays import ListCalendar, to_date

BENCH_CODE = 'sh000300'
INDEX_NAMES = dict(TREND_INDICES)
FWD_NS = (1, 3, 5, 10, 20)
# 市况分组：沪深300 截至 T 的 60 日涨跌幅 > +5% 记 up、< −5% 记 down，其余 flat（只用 ≤T 数据）
REGIME_WINDOW = 60
REGIME_BAND = 5.0
# 复盘判定需要 T 及之前的根数：σ20 + 60 个基线起点 + 5 日窗口 + T
REVIEW_BEFORE_BARS = SIGMA_WINDOW + BASELINE_DAYS + REVIEW_LOOKBACK + 1
DEFAULT_SAMPLE = 40
DEFAULT_SEED = 20261011  # 与原型抽样种子一致，便于对照
# 2：上下文/两融/量能与生产对齐（250 根、两融交易日轴 T−1、volume_adj）
ROWS_FORMAT_VERSION = 2
_CODE_RE = re.compile(r'^(sh|sz|bj)\d{6}$')


# ---------------- 配置 ----------------

def build_configs(overrides=None):
    """等价 scan.load_configs，但不读写 DB：注册表默认参数、权重 1.0，叠加 overrides。

    overrides: {dim_key: {param: value, 'weight': w, 'enabled': bool}}——调参对比用。
    未知维度、非 dict、启用未实现的维度一律 ValueError（命令层转 CommandError）。
    """
    overrides = overrides or {}
    if not isinstance(overrides, dict):
        raise ValueError('param-override 须为 {维度: {参数: 值}} 的 JSON 对象')
    unknown = sorted(set(overrides) - set(registry.DIMENSION_MAP))
    if unknown:
        raise ValueError(f'未知维度: {", ".join(unknown)}（可选: {", ".join(registry.DIMENSION_MAP)}）')
    out = []
    for spec in registry.DIMENSIONS:
        ov = overrides.get(spec['key']) or {}
        if not isinstance(ov, dict):
            raise ValueError(f'{spec["key"]} 的覆盖值须为 JSON 对象')
        ov = dict(ov)
        weight = float(ov.pop('weight', 1.0))
        enabled = bool(ov.pop('enabled', spec['enabled']))
        if enabled and spec['evaluate'] is None:
            raise ValueError(f'{spec["key"]} 没有评估实现，不能启用')
        out.append({
            'key': spec['key'],
            'name': spec['name'],
            'weight': weight,
            'params': {**spec['defaults'], **ov},
            'targets': spec['targets'],
            'evaluate': spec['evaluate'],
            'enabled': enabled,
            'defaults': spec['defaults'],
        })
    return out


def active_dims(configs):
    return [c for c in configs if c['enabled'] and c['evaluate'] is not None]


# ---------------- 注入件 ----------------

class MemoryCycleCounter:
    """内存版 _count_cycles：信号表存在内存里，交易日序列复用 signals._previous_trading_days。"""

    def __init__(self, calendar):
        self.calendar = calendar
        self.store = {}  # (kind, code) -> {date: direction}

    def record(self, kind, code, trade_date, direction):
        self.store.setdefault((kind, code), {})[to_date(trade_date)] = direction

    def __call__(self, target_kind, target_code, direction, before_date):
        days = signals._previous_trading_days(before_date, CYCLE_LOOKBACK, calendar=self.calendar)
        if days is None:
            return None
        present = self.store.get((target_kind, target_code), {})
        cycles = 0
        for day in days:
            if present.get(day) != direction:
                break
            cycles += 1
        return cycles


# ---------------- 股票池 ----------------

def parse_code(token):
    """sh600000 / 600000 / SZ000858 → 带交易所前缀的小写代码。"""
    tok = token.strip().lower()
    if _CODE_RE.match(tok):
        return tok
    if re.fullmatch(r'\d{6}', tok):
        return exchange_prefix(tok)
    raise ValueError(f'无法识别的代码: {token!r}')


def resolve_universe(store, spec, *, start, sample=DEFAULT_SAMPLE, seed=DEFAULT_SEED):
    """解析 --universe：逗号分隔，可混用
    - hs300-sample：start 当日沪深300成分按 seed 随机抽 sample 只（按时点取成分，避免幸存者偏差）；
    - hs300：当日全部成分；
    - indices：5 个主要指数（与生产宇宙一致）；
    - 具体代码：sh600000 / 600000 / sh000300（指数按 INDEX_NAMES 识别）。
    """
    targets, seen = [], set()

    def add(kind, code, name):
        if code not in seen:
            seen.add(code)
            targets.append({'kind': kind, 'code': code, 'name': name})

    for tok in [t.strip() for t in (spec or '').split(',') if t.strip()]:
        low = tok.lower()
        if low == 'indices':
            for code, name in TREND_INDICES:
                add('index', code, name)
        elif low in ('hs300-sample', 'hs300'):
            members = list(store.hs300_members(start))
            if low == 'hs300-sample':
                random.Random(seed).shuffle(members)
                members = sorted(members[:sample])
            for code, name in members:
                add('stock', parse_code(code), name)
        else:
            code = parse_code(tok)
            add('index' if code in INDEX_NAMES else 'stock', code, INDEX_NAMES.get(code, code))
    if not targets:
        raise ValueError('股票池为空')
    return targets


# ---------------- 评估辅助 ----------------

def bench_regimes(bench_items, window=REGIME_WINDOW, band=REGIME_BAND):
    """{iso_date: 'up'|'down'|'flat'}，只用截至当日的沪深300收盘价（无前视）。"""
    out = {}
    closes = [it['close'] for it in bench_items]
    for i, it in enumerate(bench_items):
        if i < window or not closes[i - window] or closes[i] is None:
            continue
        r = (closes[i] / closes[i - window] - 1) * 100
        out[it['date']] = 'up' if r > band else 'down' if r < -band else 'flat'
    return out


def outcomes(items, i, calendar, bench_by_date, fwd_ns=FWD_NS):
    """T=items[i] 的复盘判定（双向，仓库口径）与前瞻收益——仅用于评估，可以看未来。"""
    t = items[i]
    t_date = to_date(t['date'])
    before = [(to_date(b['date']), b['open'], b['close'])
              for b in items[max(0, i - REVIEW_BEFORE_BARS + 1): i + 1]]
    after = [(to_date(b['date']), b['open'], b['close']) for b in items[i + 1: i + 1 + REVIEW_LOOKBACK]]
    res = {}
    for d in ('up', 'down'):
        o = review._evaluate_bars(SimpleNamespace(trade_date=t_date, direction=d),
                                  before, after, base_is_open=True, calendar=calendar)
        res[f'hit_{d}'] = None if o is None else int(o['hit'])
        res[f'base_{d}'] = None if o is None else o['baseline']
        if d == 'up':
            res['thr'] = None if o is None else o['thr']
    # T+1 开盘买入 → T+N 收盘；要求 T 之后的 N 根正好是日历上连续 N 个交易日（停牌不算）
    ci = calendar.index_of(t['date'])
    isos = calendar.isos
    for n in fwd_ns:
        res[f'r{n}'] = res[f'b{n}'] = None
        if ci is None or i + n >= len(items) or ci + n >= len(isos):
            continue
        if items[i + 1]['date'] != isos[ci + 1] or items[i + n]['date'] != isos[ci + n]:
            continue
        entry, exit_ = items[i + 1]['open'], items[i + n]['close']
        b_entry = (bench_by_date.get(isos[ci + 1]) or {}).get('open')
        b_exit = (bench_by_date.get(isos[ci + n]) or {}).get('close')
        if not entry or exit_ is None:
            continue
        res[f'r{n}'] = (exit_ / entry - 1) * 100
        if b_entry and b_exit is not None:
            res[f'b{n}'] = (b_exit / b_entry - 1) * 100
    return res


# ---------------- 主循环 ----------------

def run_target(target, items, *, calendar, bench_by_date, configs, start, end,
               ctx_bars=DEFAULT_CTX_BARS, share_hist=None, margin_rows=None, margin_dates=None,
               counter=None, regimes=None, eval_all_days=True, with_outcomes=True):
    """单标的回放，返回行列表。counter 为 None 时新建（标的之间的周期互不影响）。"""
    kind, code, name = target['kind'], target['code'], target.get('name') or target['code']
    counter = counter or MemoryCycleCounter(calendar)
    dims = [c for c in active_dims(configs) if kind in c['targets']]
    regimes = regimes or {}
    rows = []
    for i, it in enumerate(items):
        d = it['date']
        if d < start or (end and d > end) or calendar.index_of(d) is None:
            continue
        ctx = build_ctx(kind, code, name, items, i, ctx_bars=ctx_bars, share_hist=share_hist,
                        margin_rows=margin_rows, margin_dates=margin_dates, calendar=calendar)
        if len(ctx['dates']) < MIN_PRICE_BARS:
            continue
        row = {'kind': kind, 'code': code, 'date': d, 'year': d[:4], 'regime': regimes.get(d, ''),
               'nbars': len(ctx['dates'])}
        try:
            is_calm, meta = calm_mod.is_calm(ctx)
        except Exception as e:  # noqa: BLE001 与 scan 一致：单对象脏序列只降级自己
            is_calm, meta = False, {'reason': f'平静判定异常: {e}'}
        row['calm'] = int(is_calm)
        row['calm_reason'] = '' if is_calm else (meta or {}).get('reason', '')
        if eval_all_days or is_calm:
            for cfg in dims:
                k = cfg['key']
                try:
                    r = cfg['evaluate'](ctx, cfg['params'])
                except Exception as e:  # noqa: BLE001 与 evaluate_target 一致：单维度异常不拖垮对象
                    r = {'available': False, 'direction': None, 'value': None, 'note': f'异常 {e}'}
                row[f'{k}_av'] = int(bool(r.get('available')))
                row[f'{k}_dir'] = r.get('direction') or ''
                row[f'{k}_val'] = r.get('value')
                row[f'{k}_note'] = (r.get('note') or '')[:40]
        row['sig_dir'] = ''
        row['cycles'] = 0
        row['severity'] = ''
        row['score'] = None
        if is_calm:
            sd, _dims = signals.evaluate_target(ctx, configs, cycle_counter=counter,
                                                calendar=calendar)
            if sd:
                row.update(sig_dir=sd['direction'], cycles=sd['cycles'], severity=sd['severity'],
                           score=sd['score'])
                counter.record(kind, code, d, sd['direction'])
        if with_outcomes:
            row.update(outcomes(items, i, calendar, bench_by_date))
        rows.append(row)
    return rows


def run_fingerprint(*, start, end, configs, ctx_bars, eval_all_days):
    """决定逐标的行结果是否可复用的指纹：区间、上下文长度与生效参数。"""
    payload = {
        'v': ROWS_FORMAT_VERSION, 'start': start, 'end': end, 'ctx_bars': ctx_bars,
        'eval_all_days': eval_all_days,
        'configs': {c['key']: [c['enabled'], c['weight'], c['params']]
                    for c in configs if c['evaluate'] is not None},
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]


class RowsDir:
    """逐标的行结果落盘（gzip JSON）：既是审计明细，也支持中断后 --resume 续跑。

    指纹不一致（换了区间/参数）时拒绝复用，避免把不同口径的行混进同一份报告。
    """

    def __init__(self, path, fingerprint, resume):
        self.path = Path(path)
        self.fingerprint = fingerprint
        self.path.mkdir(parents=True, exist_ok=True)
        fp_file = self.path / 'fingerprint.txt'
        old = fp_file.read_text(encoding='utf-8').strip() if fp_file.exists() else None
        if old and old != fingerprint:
            if resume:
                raise ValueError(f'{self.path} 中的行结果来自不同的区间/参数（指纹 {old}），不能 --resume')
            for f in self.path.glob('*.json.gz'):
                f.unlink()
        fp_file.write_text(fingerprint, encoding='utf-8')
        self.resume = resume

    def _file(self, code):
        return self.path / f'{code}.json.gz'

    def load(self, code):
        f = self._file(code)
        if not self.resume or not f.exists():
            return None
        with gzip.open(f, 'rt', encoding='utf-8') as fh:
            return json.load(fh)

    def save(self, code, rows):
        tmp = self.path / f'{code}.json.gz.tmp'
        with gzip.open(tmp, 'wt', encoding='utf-8') as fh:
            json.dump(rows, fh, ensure_ascii=False, default=float)
        os.replace(tmp, self._file(code))


def run_backtest(store, targets, *, start, end=None, configs=None, ctx_bars=DEFAULT_CTX_BARS,
                 eval_all_days=True, progress=None, rows_dir=None, resume=False):
    """全宇宙回放。返回 (rows, meta)。基准指数日线缺失时抛 CacheMiss（没有日历无法回测）。

    rows_dir：逐标的行结果目录；resume=True 时复用其中指纹一致的标的结果（续跑）。
    注意：周期计数按 (kind, code) 隔离，标的之间互不影响，所以逐标的复用是安全的。
    """
    configs = configs or build_configs()
    bench = store.index_bars(BENCH_CODE)
    calendar = ListCalendar([b['date'] for b in bench])
    bench_by_date = {b['date']: b for b in bench}
    regimes = bench_regimes(bench)
    margin = store.margin_rows() if any(t['kind'] == 'index' for t in targets) else []
    margin_dates = [m[0] for m in margin]
    counter = MemoryCycleCounter(calendar)
    store_rows = None
    if rows_dir:
        fp = run_fingerprint(start=start, end=end, configs=configs, ctx_bars=ctx_bars,
                             eval_all_days=eval_all_days)
        store_rows = RowsDir(rows_dir, fp, resume)
    rows, skipped, reused = [], [], 0
    for n, t in enumerate(targets, 1):
        cached = store_rows.load(t['code']) if store_rows else None
        if cached is not None:
            rows.extend(cached)
            reused += 1
            if progress:
                progress(n, len(targets), t['code'], len(cached))
            continue
        try:
            if t['kind'] == 'index':
                items = bench if t['code'] == BENCH_CODE else store.index_bars(t['code'])
                share_hist = None
            else:
                items = store.stock_bars(t['code'])
                share_hist = store.share_history(t['code'])
        except CacheMiss as e:
            skipped.append({'code': t['code'], 'reason': str(e)})
            continue
        out = run_target(t, items, calendar=calendar, bench_by_date=bench_by_date, configs=configs,
                         start=start, end=end, ctx_bars=ctx_bars, share_hist=share_hist,
                         margin_rows=margin, margin_dates=margin_dates, counter=counter,
                         regimes=regimes, eval_all_days=eval_all_days)
        rows.extend(out)
        if store_rows:
            store_rows.save(t['code'], out)
        if progress:
            progress(n, len(targets), t['code'], len(out))
    meta = {
        'start': start, 'end': end or calendar.isos[-1],
        'calendar': [calendar.isos[0], calendar.isos[-1]],
        'ctx_bars': ctx_bars,
        'targets_requested': len(targets),
        'targets_run': len(targets) - len(skipped),
        'skipped': skipped,
        'targets_reused': reused,
        'data_notes': list(dict.fromkeys(store.notes + ([MARGIN_PROXY_NOTE] if margin else []))),
        'shares_missing': sorted({m for m in store.missing if m.startswith('shares/')}),
        'configs': {c['key']: {'enabled': c['enabled'], 'weight': c['weight'], 'params': c['params']}
                    for c in configs if c['evaluate'] is not None},
    }
    return rows, meta
