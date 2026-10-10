/** Shared display helpers for prices / pct (A-share UI). */

export function formatPct(val) {
  if (val == null) return '-'
  const num = Number(val)
  if (Number.isNaN(num)) return '-'
  const sign = num > 0 ? '+' : ''
  return `${sign}${num.toFixed(2)}%`
}

export function formatNum(val, digits = 2) {
  if (val == null) return '-'
  const num = Number(val)
  if (Number.isNaN(num)) return '-'
  return num.toFixed(digits)
}

/** 带正负号的数字（涨跌额等），null/非数字显示 '-' */
export function formatSigned(val, digits = 2) {
  if (val == null) return '-'
  const num = Number(val)
  if (Number.isNaN(num)) return '-'
  const sign = num > 0 ? '+' : ''
  return `${sign}${num.toFixed(digits)}`
}

/**
 * 相对基准价的涨跌幅（%）。base 缺失或为 0 时返回 null，由调用方显示 '-'。
 * A 股"涨跌幅"口径：基准为昨收（prev_close），不是开盘价。
 */
export function pctFrom(price, base) {
  if (price == null || base == null) return null
  const p = Number(price)
  const b = Number(base)
  if (Number.isNaN(p) || Number.isNaN(b) || b === 0) return null
  return (p - b) / b * 100
}

export function pctClass(val) {
  if (val == null) return ''
  const num = Number(String(val).replace('%', ''))
  if (Number.isNaN(num)) return ''
  if (num > 0) return 'up'
  if (num < 0) return 'down'
  return ''
}

export function formatDateTime(dt) {
  if (!dt) return '-'
  return String(dt).replace('T', ' ').slice(0, 19)
}
