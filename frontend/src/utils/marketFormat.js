/** 市场页共用数字展示 */
export function formatYi(val, digits = 2) {
  if (val == null || Number.isNaN(Number(val))) return '-'
  const n = Number(val)
  return `${n >= 0 ? '+' : ''}${n.toFixed(digits)}`
}

const AMOUNT_UNITS = [[1e12, '万亿'], [1e8, '亿'], [1e4, '万']]

/**
 * 金额统一格式化（输入单位：元）→ 万/亿/万亿，带单位后缀。
 * - 先按目标精度四舍五入再选单位，避免 99,999,999.9 显示成 "10000.00 万"
 * - signed=true 时正数加 "+"（资金净流入等场景）
 */
export function formatAmount(val, { signed = false, digits = 2 } = {}) {
  if (val == null || val === '' || Number.isNaN(Number(val))) return '-'
  const n = Number(val)
  const a = Math.abs(n)
  let body = `${Math.round(a)}`
  for (let i = 0; i < AMOUNT_UNITS.length; i++) {
    const [u, label] = AMOUNT_UNITS[i]
    // 下一档（更小单位）四舍五入后会进位到本档时，直接用本档
    const lowerStep = i + 1 < AMOUNT_UNITS.length ? AMOUNT_UNITS[i + 1][0] / 10 ** digits : 1
    if (a >= u - lowerStep / 2) {
      body = `${(a / u).toFixed(digits)} ${label}`
      break
    }
  }
  const isZero = !/[1-9]/.test(body)
  const sign = isZero ? '' : n < 0 ? '-' : signed ? '+' : ''
  return `${sign}${body}`
}

/** 图表坐标轴用：元 → 亿（整数，带"亿"字），如 -5234567890 → "-52亿" */
export function formatYiAxis(val) {
  if (val == null || Number.isNaN(Number(val))) return ''
  const v = Math.round(Number(val) / 1e8)
  return `${v === 0 ? 0 : v}亿`
}

/** ETF 份额：份 → 亿份/万份 */
export function formatShare(val) {
  if (val == null || Number.isNaN(Number(val))) return '-'
  const n = Number(val)
  const a = Math.abs(n)
  if (a >= 1e8) return `${(n / 1e8).toFixed(2)} 亿份`
  if (a >= 1e4) return `${(n / 1e4).toFixed(2)} 万份`
  return `${n.toFixed(0)} 份`
}

export function formatTurnover(val) {
  return formatAmount(val)
}
