/**
 * 选股结果表的附加列与单元格格式（ARC-09：从 Screener.vue 抽出的纯函数）。
 * 附加列 = 条件与排序里用到、且固定列没展示的指标。
 */
import { BOOL_FIELDS, FIELDS } from '../constants/screenerFields.js'
import { formatNum, formatPct, formatRate } from './format.js'
import { formatAmount, formatVolume } from './marketFormat.js'

export const FIXED_COLS = new Set(['change_pct', 'close_price', 'high_low_pct', 'turnover'])

export function extraColumnsFor(spec) {
  if (!spec) return []
  const keys = []
  for (const c of spec.conditions || []) {
    if (!FIXED_COLS.has(c.field) && !keys.includes(c.field)) keys.push(c.field)
  }
  if (spec.order_by && !FIXED_COLS.has(spec.order_by) && !keys.includes(spec.order_by)) {
    keys.push(spec.order_by)
  }
  return keys.map(k => ({
    key: k,
    label: (FIELDS.find(f => f.key === k) || {}).label || k,
    bool: BOOL_FIELDS.has(k),
  }))
}

// 按字段类型选格式（成交量带单位、成交额换算万/亿、振幅不带正号）
const AMOUNT_FIELDS = new Set(['turnover', 'turnover_5d_avg', 'float_mv', 'total_mv'])
// 非负比率：不加正号
const RATE_FIELDS = new Set(['high_low_pct', 'turnover_rate'])
const PCT_FIELDS = new Set(['change_pct', 'open_close_pct', 'pct_5d', 'pct_10d', 'pct_20d', 'pct_60d', 'pct_from_high_20d'])

export function fmtCell(row, col) {
  const v = row[col.key]
  if (v == null) return '-'
  if (col.bool) return v === 1 ? '是' : '否'
  if (col.key === 'volume') return formatVolume(v)
  if (col.key === 'up_days') return String(v)
  if (RATE_FIELDS.has(col.key)) return formatRate(v)
  if (AMOUNT_FIELDS.has(col.key)) return formatAmount(v)
  if (PCT_FIELDS.has(col.key)) return formatPct(v)
  return formatNum(v)
}
