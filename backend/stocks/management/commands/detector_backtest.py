"""探测器离线回测：逐日按时点回放平静闸门、维度投票与复盘判定，输出评估报告。

不写库（不碰 DetectorSignal / DetectorDimConfig），只输出 report.json + report.md
和逐标的行明细（rows/，可 --resume 续跑）。数据走本地文件缓存，--offline 只读缓存。

用法：
    python manage.py detector_backtest --start 2023-10-09 --end 2026-10-09 \\
        --universe hs300-sample,indices --sample 40 --seed 20261011 --offline \\
        --cache-dir ../../detector-research/backtest/data --out /tmp/bt_base
    # 调参对比：同区间同股票池，只覆盖维度参数
    python manage.py detector_backtest ... --param-override '{"vol_compress": {"drift_z": 1.8}}'
    # 植入形态检测力（合成数据，不需要缓存）
    python manage.py detector_backtest --synthetic 20 --universe none --out /tmp/bt_syn
"""

import json
import time
from datetime import date, datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


def _iso(value):
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise CommandError(f'日期格式须为 YYYY-MM-DD: {value!r}') from None


def parse_override(raw):
    """--param-override：JSON 字符串，或 @path 指向 JSON 文件。"""
    if not raw:
        return {}
    text = raw
    if raw.startswith('@'):
        p = Path(raw[1:])
        if not p.exists():
            raise CommandError(f'参数覆盖文件不存在: {p}')
        text = p.read_text(encoding='utf-8')
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise CommandError(f'--param-override 不是合法 JSON: {e}') from None
    if not isinstance(data, dict):
        raise CommandError('--param-override 须为 {维度: {参数: 值}} 的 JSON 对象')
    return data


class Command(BaseCommand):
    help = '探测器离线回测与评估（不写库；输出 JSON + Markdown 报告）'

    def add_arguments(self, parser):
        parser.add_argument('--start', default='2023-10-09', help='评估起始日（含），默认 2023-10-09')
        parser.add_argument('--end', default=None, help='评估截止日（含），默认缓存中的最新交易日')
        parser.add_argument('--universe', default='hs300-sample,indices',
                            help='逗号分隔：hs300-sample / hs300 / indices / 代码（sh600000、600000、sh000300）；'
                                 'none 表示不跑真实数据（只做 --synthetic）')
        parser.add_argument('--sample', type=int, default=40, help='hs300-sample 抽样只数，默认 40')
        parser.add_argument('--seed', type=int, default=20261011, help='抽样与重采样种子')
        parser.add_argument('--offline', action='store_true', help='只用本地缓存，不联网')
        parser.add_argument('--refresh', action='store_true', help='联网整段重拉缓存（前复权价会重新缩放）')
        parser.add_argument('--cache-dir', default=None, help='缓存目录，默认 backend/.cache/detector_backtest/')
        parser.add_argument('--out', default=None, help='输出目录（report.json / report.md / rows/）')
        parser.add_argument('--param-override', default=None,
                            help='JSON 或 @文件：{维度: {参数: 值, "weight": w, "enabled": bool}}')
        parser.add_argument('--ctx-bars', type=int, default=None, help='上下文根数，默认与生产一致（70）')
        parser.add_argument('--resamples', type=int, default=2000, help='随机基线重采样次数，0 只给解析式 z')
        parser.add_argument('--calm-only', action='store_true',
                            help='只在平静日评估维度（更快，但没有「无闸门」对照）')
        parser.add_argument('--resume', action='store_true', help='复用 out/rows/ 中指纹一致的标的结果续跑')
        parser.add_argument('--synthetic', type=int, default=0, help='植入形态检测力测试的序列条数，0 跳过')

    def handle(self, *args, **opt):
        from stocks.detector.backtest import data, engine, metrics, report, synthetic
        from stocks.detector.backtest.context import DEFAULT_CTX_BARS

        start = _iso(opt['start'])
        end = _iso(opt['end']) if opt['end'] else None
        if end and end < start:
            raise CommandError('--end 早于 --start')
        if opt['sample'] <= 0:
            raise CommandError('--sample 须为正整数')
        if opt['resamples'] < 0:
            raise CommandError('--resamples 不能为负')
        ctx_bars = opt['ctx_bars'] or DEFAULT_CTX_BARS
        if ctx_bars < 30:
            raise CommandError('--ctx-bars 不能少于 30（MIN_PRICE_BARS）')
        if (opt['universe'] or '').strip().lower() == 'none' and not opt['synthetic']:
            raise CommandError('--universe none 时须指定 --synthetic N')
        override = parse_override(opt['param_override'])
        try:
            configs = engine.build_configs(override)
        except ValueError as e:
            raise CommandError(str(e)) from None

        out = Path(opt['out']) if opt['out'] else (
            data.default_cache_dir() / 'reports' / f"{start}_{end or 'latest'}_{datetime.now():%Y%m%d%H%M%S}")
        out.mkdir(parents=True, exist_ok=True)
        dims = [c['key'] for c in engine.active_dims(configs)]
        t0 = time.time()
        universe = (opt['universe'] or '').strip()
        summary = {'meta': {'rows': 0}, 'dims': {}, 'signals': {}}
        run_meta = {'universe': universe, 'ctx_bars': ctx_bars, 'param_override': override or None,
                    'offline': opt['offline']}

        if universe.lower() != 'none':
            fetch_end = end or datetime.now().strftime('%Y-%m-%d')
            store = data.DataStore(opt['cache_dir'], offline=opt['offline'], refresh=opt['refresh'],
                                   fetch_end=fetch_end)
            try:
                targets = engine.resolve_universe(store, universe, start=start, sample=opt['sample'],
                                                  seed=opt['seed'])
            except (ValueError, data.CacheMiss) as e:
                raise CommandError(f'股票池解析失败: {e}') from None

            def progress(n, total, code, nrows):
                self.stdout.write(f'[{n}/{total}] {code} {nrows} 行 {time.time() - t0:.0f}s')

            try:
                rows, meta = engine.run_backtest(
                    store, targets, start=start, end=end, configs=configs, ctx_bars=ctx_bars,
                    eval_all_days=not opt['calm_only'], progress=progress,
                    rows_dir=out / 'rows', resume=opt['resume'],
                )
            except data.CacheMiss as e:
                raise CommandError(f'基准指数日线缺失，无法构造交易日历: {e}') from None
            except ValueError as e:
                raise CommandError(str(e)) from None
            run_meta.update(meta)
            summary = metrics.summarize(rows, dims, n_resample=opt['resamples'], seed=opt['seed'])

        if opt['synthetic']:
            summary['synthetic'] = synthetic.power_test(configs, n_series=opt['synthetic'], seed=opt['seed'] % 997)

        summary['run'] = run_meta
        summary['run']['elapsed_s'] = round(time.time() - t0, 1)
        (out / 'report.json').write_text(
            json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
        (out / 'report.md').write_text(report.render(summary, run_meta), encoding='utf-8')
        self.stdout.write(self.style.SUCCESS(f'回测完成：{out}/report.md（{summary["run"]["elapsed_s"]}s）'))
