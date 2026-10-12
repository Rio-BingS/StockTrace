// 探测器维度配置表的展示口径（集成：data-align #6 #9、thresholds #1、calm-vote #4、new-dims）。
// 适用对象、投票门槛、停用原因都取后端 /detector/config 返回的字段，不在前端手写
// 映射——旧实现把板块资金净额写成「指数/板块」、融资余额写成「指数」，与实际不符。

const TARGET_LABEL = { index: '指数', stock: '自选股', industry: '板块', concept: '板块' }
const ALL_TARGETS = ['index', 'stock', 'industry', 'concept']

/** 维度适用对象：融资余额只在沪深300 投票；四类全覆盖记「全部」；缺字段为 '-' */
export function dimScopeText(dim) {
  const d = dim || {}
  if (Array.isArray(d.vote_codes) && d.vote_codes.length) {
    return d.vote_codes.map((c) => (c === 'sh000300' ? '沪深300' : c)).join('/')
  }
  const targets = Array.isArray(d.targets) ? d.targets : []
  if (!targets.length) return '-'
  if (ALL_TARGETS.every((t) => targets.includes(t))) return '全部'
  const labels = []
  for (const t of targets) {
    const label = TARGET_LABEL[t] || t
    if (!labels.includes(label)) labels.push(label)
  }
  return labels.join('/')
}

const isNum = (v) => typeof v === 'number' && Number.isFinite(v)

/** 投票门槛：价格类维度用 z 区间（z_lo, z_hi]，经验闸门类维度仍显示 min */
export function dimThresholdText(dim) {
  const p = dim?.params || {}
  if (isNum(p.z_lo) && isNum(p.z_hi)) return `z ${p.z_lo}~${p.z_hi}`
  // det-v2 只展示/只作否决的维度：截面分位门槛与解禁比例门槛
  if (isNum(p.veto_pct)) return `分位 ≥${p.veto_pct}`
  if (isNum(p.tag_pct)) return `分位 ≥${p.tag_pct}`
  if (isNum(p.min_ratio)) return `≥${(p.min_ratio * 100).toFixed(0)}%`
  return isNum(p.min) ? String(p.min) : '-'
}

/** 停用类别：候选维度（已实现、回测未证明有效）与无数据源分开说 */
export const DISABLED_KIND_LABEL = {
  candidate: '候选（停用）',
  no_source: '无数据源',
}

export function disabledText(dim) {
  return DISABLED_KIND_LABEL[dim?.disabled_kind] || '停用'
}

/** 投票规则一句话说明；字段缺失（旧后端）返回 '' 不显示 */
export function voteRuleText(rules) {
  const r = rules || {}
  if (!isNum(r.min_dims) || !isNum(r.min_weight) || !isNum(r.oppose_ratio)) return ''
  const groups = Object.entries(r.groups || {})
  const dirGroups = groups.filter(([, g]) => g.role === 'direction').length
  const strength = groups.filter(([, g]) => g.role === 'strength').length
  let text =
    `出信号需同侧至少 ${r.min_dims} 个信息组投票、加权票 ≥ ${r.min_weight}，` +
    `且对侧加权票 < 同侧 × ${r.oppose_ratio}`
  if (groups.length) {
    text += `；${dirGroups} 个方向组决定涨跌，${strength} 个量能组只在活跃度抬升时作强度确认`
  }
  if (isNum(r.sector_signal_cap)) {
    text +=
      r.sector_signal_cap > 0
        ? `；板块信号每日按强度最多保留 ${r.sector_signal_cap} 个`
        : '；板块信号不截断'
  }
  return text + '。'
}
