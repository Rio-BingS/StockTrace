// 探测器复盘指标的展示格式（#7/#12）。
// 绝对命中率在零假设下也有约 20%（旧口径约 45%），单看它会误读成有效性，
// 所以页面并列展示「命中 / 基线 / 超额 / 独立样本数」。旧扫描记录没有基线等
// 字段（后端返回 null），一律显示「-」，不报错。

const isNum = (v) => typeof v === 'number' && Number.isFinite(v)

/** 比率（0~1）→ 百分比整数，如 0.453 → '45%'；缺失为 '-' */
export function fmtRate(v) {
  return isNum(v) ? `${(v * 100).toFixed(0)}%` : '-'
}

/** 超额命中率（命中率 − 基线，0~1 的差）→ 百分点，如 0.12 → '+12pp' */
export function fmtExcessRate(v) {
  if (!isNum(v)) return '-'
  const pp = Math.round(v * 100)
  return `${pp > 0 ? '+' : ''}${pp}pp`
}

/** 收益/超额收益（已是 %）→ '+1.23%'；缺失为 '-' */
export function fmtReturn(v, digits = 2) {
  if (!isNum(v)) return '-'
  return `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`
}

/** 正负着色：A 股习惯红涨绿跌，与页面方向色一致 */
export function signClass(v) {
  if (!isNum(v) || v === 0) return ''
  return v > 0 ? 'up' : 'down'
}

/** 扫描记录的复盘四件套 */
export function runReviewCells(run) {
  const r = run || {}
  const samples = isNum(r.independent_samples) ? String(r.independent_samples) : '-'
  return {
    hit: fmtRate(r.hit_rate),
    baseline: fmtRate(r.baseline_rate),
    excess: fmtExcessRate(r.excess_rate),
    excessClass: signClass(r.excess_rate),
    samples,
    reviewed: isNum(r.reviewed_count) ? r.reviewed_count : 0,
  }
}

/** 信号方向调整后的相对基准超额（向下信号跑输基准才算对） */
export function directionalExcess(signal) {
  const s = signal || {}
  if (!isNum(s.excess_return)) return null
  return s.direction === 'down' ? -s.excess_return : s.excess_return
}

/** 回看基期说明：板块快照没有开盘价，只能用 T+1 收盘 */
export const REVIEW_BASE_TEXT = {
  open_t1: '次日（T+1）开盘',
  close_t1: '次日（T+1）收盘',
}

export function baseText(base, kind) {
  const key = base?.[kind]
  return REVIEW_BASE_TEXT[key] || '-'
}

/** 调参流水的参数变更 {k: [旧, 新]} → 'weight 1→1.2、noise_pct 0.8→0.75' */
export function fmtChanges(changes) {
  const entries = Object.entries(changes || {})
  if (!entries.length) return '-'
  return entries
    .map(([k, v]) => (Array.isArray(v) ? `${k} ${v[0]}→${v[1]}` : `${k} ${v}`))
    .join('、')
}

export const TUNE_SOURCE_LABEL = {
  auto: '已生效',
  dry_run: '演练（未生效）',
  legacy: '旧流水',
}
