import { formatPct } from './format.js'
import { formatAmount } from './marketFormat.js'

export function signedAmount(value) {
  return formatAmount(value, { signed: true })
}

export function metricWidth(value) {
  if (value == null || Number.isNaN(Number(value))) return '0%'
  return `${Math.min(100, Math.max(0, Number(value)))}%`
}

export function flowPath(side, index) {
  const rowY = 88 + index * 52
  const hubY = 164 + index * 10
  return side === 'out'
    ? `M 430 ${rowY} C 474 ${rowY}, 466 ${hubY}, 505 ${hubY}`
    : `M 695 ${hubY} C 734 ${hubY}, 726 ${rowY}, 770 ${rowY}`
}

export function rowTitle(row) {
  return [
    row.name,
    `净额 ${signedAmount(row.net)}`,
    `流入 ${formatAmount(row.inflow)}`,
    `流出 ${formatAmount(row.outflow)}`,
    `涨跌 ${formatPct(row.change_pct)}`,
    `领涨 ${row.leader || '-'} ${formatPct(row.leader_pct)}`,
  ].join('\n')
}

export function netToneLabel(netTotal) {
  const value = Number(netTotal || 0)
  if (value > 0) return '净流入'
  if (value < 0) return '净流出'
  return '资金平衡'
}

export function buildInsights(summary, divergenceCount) {
  const concentration = summary.top_three_inflow_concentration_pct
  const breadth = summary.breadth_pct
  return [
    {
      tone: 'green',
      title: concentration == null ? '集中度待更新' : concentration >= 65 ? '主线集中' : '轮动较均衡',
      body: concentration == null
        ? '当前上游未提供可计算样本。'
        : `流入前三集中度 ${concentration.toFixed(1)}%，${concentration >= 65 ? '资金更偏向少数主线。' : '资金分布相对均衡。'}`,
    },
    {
      tone: 'amber',
      title: breadth == null ? '广度待更新' : breadth >= 50 ? '流入占优' : '流出占优',
      body: breadth == null
        ? '当前上游未提供可计算样本。'
        : `资金广度 ${breadth.toFixed(1)}%，${breadth >= 50 ? '净流入板块占多数。' : '净流出板块占多数。'}`,
    },
    {
      tone: 'blue',
      title: divergenceCount ? '存在价流背离' : '价流暂未背离',
      body: divergenceCount
        ? `发现 ${divergenceCount} 个显著价流背离板块，需结合后续价格确认。`
        : '当前横截面未发现显著价流背离。',
    },
  ]
}
