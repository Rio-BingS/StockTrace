"""把 metrics.summarize 的结果渲染成 Markdown 报告。"""

from __future__ import annotations

import math

_KIND = {'stock': '个股', 'index': '指数', 'all': '全部'}


def _fmt(x, p=1, pct=False):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return '—'
    return f'{x * 100:.{p}f}%' if pct else f'{x:.{p}f}'


def _p(x):
    if x is None:
        return '—'
    return f'{x:.3f}' if x >= 0.001 else '<0.001'


def _fwd(c, n, key='mean'):
    return _fmt((c.get(f'fwd{n}') or {}).get(key), 2)


def _synthetic_lines(syn):
    if not syn:
        return []
    L = ['', '## 植入形态检测力（合成数据）', '',
         f"随机游走 {syn['series']} 条 × {syn['days']} 日，每条在后半段植入 1 次形态；对照组为同一随机冲击、未植入的序列。", '',
         '| 形态 | 植入窗口内同向信号率 | 对照同窗口信号率 | 植入窗口内任一维度同向触发率 | 对照 |', '|---|---|---|---|---|']
    for name, r in syn['patterns'].items():
        L.append(f"| {name} | {_fmt(r['planted_signal_rate'], 1, True)} | {_fmt(r['null_signal_rate'], 1, True)} | "
                 f"{_fmt(r['planted_dim_rate'], 1, True)} | {_fmt(r['null_dim_rate'], 1, True)} |")
    return L


def render(summary, run_meta=None):
    run_meta = run_meta or {}
    m = summary['meta']
    L = ['# 探测器离线回测报告', '']
    if not m.get('rows'):
        if not summary.get('synthetic'):
            L.append('没有可评估的样本（检查区间、股票池与缓存）。')
        L += _synthetic_lines(summary.get('synthetic'))
        return '\n'.join(L) + '\n'
    L.append(f"- 区间：{m['period'][0]} ~ {m['period'][1]}；{m['targets']} 个标的，{m['rows']} 个标的·日")
    if run_meta:
        L.append(f"- 股票池：{run_meta.get('universe', '—')}；上下文 {run_meta.get('ctx_bars')} 根；"
                 f"重采样 {m.get('resamples')} 次（seed={m.get('seed')}）")
        if run_meta.get('param_override'):
            L.append(f"- 参数覆盖：`{run_meta['param_override']}`")
        if run_meta.get('skipped'):
            L.append(f"- 跳过（缓存缺失）：{', '.join(s['code'] for s in run_meta['skipped'])}")
        if run_meta.get('shares_missing'):
            L.append(f"- 流通股本缺失（换手率退回成交量分位）：{len(run_meta['shares_missing'])} 只")
        for note in run_meta.get('data_notes') or []:
            L.append(f'- 数据说明：{note}')
    L.append('- 平静通过率：' + '，'.join(f"{_KIND.get(k, k)} {_fmt(v, 1, True)}" for k, v in m['calm_rate'].items()))
    if m.get('m_tests'):
        L.append(f"- 多重比较：单维度检验 {m['m_tests']} 次，Bonferroni 阈值 p<{m['bonferroni_alpha']:.4f}")
    L.append('- 显著性未校正样本重叠与截面相关，偏乐观；样本小的格子只作方向性线索。')

    L += ['', '## 信号层（≥2 维同向且零反向，平静日）', '',
          '| 对象 | 口径 | n(↑/↓) | 命中率 | 仓库基线 | 随机基线 | 超额 | z | 重采样 p | 5日超额 | 10日超额 | 20日超额 | 随机20日 | 20日胜率 |',
          '|---|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for kind, d in summary['signals'].items():
        for lab in ('all_signals', 'active', 'segment_heads'):
            c = d[lab]
            L.append(f"| {_KIND.get(kind, kind)} | {lab} | {c['n']}({c['n_up']}/{c['n_down']}) | "
                     f"{_fmt(c.get('hit_rate'), 1, True)} | {_fmt(c.get('repo_baseline'), 1, True)} | "
                     f"{_fmt(c.get('rand_baseline'), 1, True)} | {_fmt(c.get('rand_excess'), 1, True)} | "
                     f"{_fmt(c.get('z'), 2)} | {_p(c.get('p_resample'))} | {_fwd(c, 5)} | {_fwd(c, 10)} | "
                     f"{_fwd(c, 20)} | {_fwd(c, 20, 'rand_mean')} | {_fmt((c.get('fwd20') or {}).get('win'), 0, True)} |")
    sev = summary['signals'].get('all', {}).get('severity')
    if sev:
        L.append('')
        L.append('严重度分布：' + '，'.join(f'{k or "—"} {v}' for k, v in sev.items()))

    L += ['', '## 单维度（平静日触发，生产语境）', '',
          '| 维度 | 对象 | 平静日投票率 | 触发 n(↑/↓) | 段首 n | 命中率 | 仓库基线 | 随机基线 | 超额 | z | 重采样 p | 5日超额 | 10日超额 | 20日超额 | 随机20日 | 20日胜率 | IC20(平静) |',
          '|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for key, v in summary['dims'].items():
        k, kind = key.split('|')
        c = v['calm']
        ic = (v.get('ic_calm') or {}).get(20)
        L.append(f"| {k} | {_KIND.get(kind, kind)} | {_fmt(v['vote_rate_calm'], 1, True)} | {c['n']}({c['n_up']}/{c['n_down']}) | "
                 f"{v['calm_heads']['n']} | {_fmt(c.get('hit_rate'), 1, True)} | {_fmt(c.get('repo_baseline'), 1, True)} | "
                 f"{_fmt(c.get('rand_baseline'), 1, True)} | {_fmt(c.get('rand_excess'), 1, True)} | {_fmt(c.get('z'), 2)} | "
                 f"{_p(c.get('p_resample'))} | {_fwd(c, 5)} | {_fwd(c, 10)} | {_fwd(c, 20)} | {_fwd(c, 20, 'rand_mean')} | "
                 f"{_fmt((c.get('fwd20') or {}).get('win'), 0, True)} | {_fmt((ic or {}).get('ic'), 3)} |")

    L += ['', '## 单维度段首（去重叠）与无闸门对照', '',
          '| 维度 | 对象 | 段首 n | 段首命中率 | 段首随机基线 | 段首 p | 段首20日超额 | 无闸门 n | 无闸门命中率 | 无闸门随机基线 | 无闸门 p | 无闸门20日超额 | IC20(全部日) |',
          '|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for key, v in summary['dims'].items():
        k, kind = key.split('|')
        h, a = v['calm_heads'], v['nogate']
        ic = (v.get('ic_all') or {}).get(20)
        L.append(f"| {k} | {_KIND.get(kind, kind)} | {h['n']} | {_fmt(h.get('hit_rate'), 1, True)} | "
                 f"{_fmt(h.get('rand_baseline'), 1, True)} | {_p(h.get('p_resample'))} | {_fwd(h, 20)} | {a['n']} | "
                 f"{_fmt(a.get('hit_rate'), 1, True)} | {_fmt(a.get('rand_baseline'), 1, True)} | {_p(a.get('p_resample'))} | "
                 f"{_fwd(a, 20)} | {_fmt((ic or {}).get('ic'), 3)} |")

    for title, field in (('按年份', 'by_year'), ('按市况（沪深300 60日涨跌 ±5%）', 'by_regime')):
        L += ['', f'## 信号与单维度{title}（平静日触发）', '',
              '| 对象 | 分组 | n(↑/↓) | 命中率 | 随机基线 | 超额 | 20日超额 | 随机20日 |', '|---|---|---|---|---|---|---|---|']
        rows = [(f"信号·{_KIND.get(kind, kind)}", d.get(field) or {}) for kind, d in summary['signals'].items()]
        rows += [(f"{key.split('|')[0]}·{_KIND.get(key.split('|')[1], key.split('|')[1])}", v.get(field) or {})
                 for key, v in summary['dims'].items() if not key.endswith('|all')]
        for label, groups in rows:
            for g, c in groups.items():
                L.append(f"| {label} | {g} | {c['n']}({c['n_up']}/{c['n_down']}) | {_fmt(c.get('hit_rate'), 1, True)} | "
                         f"{_fmt(c.get('rand_baseline'), 1, True)} | {_fmt(c.get('rand_excess'), 1, True)} | "
                         f"{_fmt(c.get('fwd20_mean'), 2)} | {_fmt(c.get('fwd20_rand'), 2)} |")

    L += _synthetic_lines(summary.get('synthetic'))
    if m.get('calm_reasons'):
        L += ['', '平静未通过首因（前 8）：' + '；'.join(f'{k}×{v}' for k, v in m['calm_reasons'].items())]
    return '\n'.join(L) + '\n'
