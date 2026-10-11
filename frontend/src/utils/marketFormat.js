/** 市场页金额 / 份额展示。实现统一在 number.js（FEF-11），这里保留原有函数名。 */
import { fmtAmount, fmtShare, fmtVolume, toNum } from './number.js'

/**
 * 金额统一格式化 → 万/亿/万亿，带单位后缀。
 * - unit：输入值的单位（默认元；两融、北向等接口给的是亿元时传 '亿'）
 * - 先换算成元、四舍五入后再选单位，避免 99,999,999.9 显示成 "10000.00 万"
 * - signed=true 时正数加 "+"（资金净流入等有方向的金额）
 */
export function formatAmount(val, { signed = false, digits = 2, unit = '元' } = {}) {
  return fmtAmount(val, { signed, digits, unit })
}

/** 图表坐标轴用：元 → 亿（整数，带"亿"字），如 -5234567890 → "-52亿" */
export function formatYiAxis(val) {
  const n = toNum(val)
  if (n == null) return ''
  const v = Math.round(n / 1e8)
  return `${v === 0 ? 0 : v}亿`
}

/** ETF 份额：份 → 亿份/万份；份额变化传 signed=true */
export function formatShare(val, { signed = false } = {}) {
  return fmtShare(val, { signed })
}

/** 成交量 / 持股数：股（或手）→ 万股/亿股 */
export function formatVolume(val, { unit = '股', signed = false } = {}) {
  return fmtVolume(val, { unit, signed })
}

export function formatTurnover(val) {
  return formatAmount(val)
}
