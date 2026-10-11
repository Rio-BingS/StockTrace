/**
 * 全站数字格式化的唯一实现（FEF-11）。format.js / marketFormat.js 只是对这里的薄封装。
 *
 * 约定：
 * - 空值（null / undefined / '' / 纯空白 / NaN / ±Infinity）一律显示 '-'，不会被当成 0
 * - 正负号按「四舍五入后」的值判断：0.004 → "0.00"，-0.004 → "0.00"（不出现 "+0.00" / "-0.00"）
 * - 0 不带正号
 * - 带单位的数值先换算到基础单位（元 / 股 / 份），按目标精度四舍五入后再选档位（万 / 亿 / 万亿），
 *   避免 99,999,999.9 显示成 "10000.00 万"
 * - 数字与档位单位之间留一个空格："1.23 亿"
 */

/** 转成有限数字；空值、非数字、非有限值返回 null */
export function toNum(val) {
  if (val == null) return null
  if (typeof val === 'string' && val.trim() === '') return null
  const n = typeof val === 'number' ? val : Number(val)
  return Number.isFinite(n) ? n : null
}

/** 按 digits 位四舍五入后的值（-0 归一为 0） */
export function roundTo(n, digits = 2) {
  const r = Number(n.toFixed(digits))
  return r === 0 ? 0 : r
}

/** 舍入后的符号：1 / -1 / 0，供着色与正负号共用 */
export function signOf(val, digits = 2) {
  const n = toNum(val)
  if (n == null) return 0
  return Math.sign(roundTo(n, digits))
}

/** 固定小数位，不带正号（负数保留 "-"，舍入为 0 时不带 "-"） */
export function fmtFixed(val, digits = 2) {
  const n = toNum(val)
  if (n == null) return '-'
  return roundTo(n, digits).toFixed(digits)
}

/** 带正负号的固定小数位：涨跌额、盈亏等有方向的数值 */
export function fmtSigned(val, digits = 2) {
  const n = toNum(val)
  if (n == null) return '-'
  const r = roundTo(n, digits)
  return `${r > 0 ? '+' : ''}${r.toFixed(digits)}`
}

/** 有方向的百分比（涨跌幅）：带正负号 */
export function fmtPct(val, digits = 2) {
  const s = fmtSigned(val, digits)
  return s === '-' ? s : `${s}%`
}

/** 非负比率（振幅、换手率、持股比例）：不加正号，调用方也不应着色 */
export function fmtRate(val, digits = 2) {
  const s = fmtFixed(val, digits)
  return s === '-' ? s : `${s}%`
}

const TIERS = [
  [1e12, '万亿'],
  [1e8, '亿'],
  [1e4, '万'],
]

/**
 * 基础单位数值 → 万 / 亿 / 万亿 档位文本。
 * suffix 拼在档位后面（"股" → "1.23 万股"）；不足 1 万时按整数显示并带 baseLabel（可为空）。
 */
function fmtTiered(n, { digits = 2, signed = false, suffix = '', baseLabel = '' } = {}) {
  const a = Math.abs(n)
  let body = null
  for (let i = 0; i < TIERS.length; i++) {
    const [u, label] = TIERS[i]
    // 下一档四舍五入后会进位到本档时，直接用本档
    const lowerStep = i + 1 < TIERS.length ? TIERS[i + 1][0] / 10 ** digits : 1
    if (a >= u - lowerStep / 2) {
      body = `${(a / u).toFixed(digits)} ${label}${suffix}`
      break
    }
  }
  if (body == null) {
    const int = `${Math.round(a)}`
    body = baseLabel ? `${int} ${baseLabel}` : int
  }
  const isZero = !/[1-9]/.test(body)
  const sign = isZero ? '' : n < 0 ? '-' : signed ? '+' : ''
  return `${sign}${body}`
}

const AMOUNT_FACTORS = { 元: 1, 万: 1e4, 亿: 1e8 }

/**
 * 金额 → "523.46 亿"。unit 为输入值的单位（元 / 万 / 亿），先换算成元再选档位。
 * signed=true 时正数加 "+"（净流入、盈亏等有方向的金额）。
 */
export function fmtAmount(val, { unit = '元', signed = false, digits = 2 } = {}) {
  const n = toNum(val)
  if (n == null) return '-'
  const factor = AMOUNT_FACTORS[unit]
  if (factor == null) throw new Error(`fmtAmount: 不支持的单位 ${unit}`)
  return fmtTiered(n * factor, { digits, signed })
}

const VOLUME_FACTORS = { 股: 1, 手: 100 }

/** 成交量 / 持股数 → "1.23 万股"。unit 为输入单位（股 / 手），统一按股显示 */
export function fmtVolume(val, { unit = '股', signed = false, digits = 2 } = {}) {
  const n = toNum(val)
  if (n == null) return '-'
  const factor = VOLUME_FACTORS[unit]
  if (factor == null) throw new Error(`fmtVolume: 不支持的单位 ${unit}`)
  return fmtTiered(n * factor, { digits, signed, suffix: '股', baseLabel: '股' })
}

/** ETF 份额（份）→ "1.23 亿份"；份额变化传 signed=true */
export function fmtShare(val, { signed = false, digits = 2 } = {}) {
  const n = toNum(val)
  if (n == null) return '-'
  return fmtTiered(n, { digits, signed, suffix: '份', baseLabel: '份' })
}

/** 涨跌着色 class：按舍入后的值判断，与显示文本保持一致（"0.00%" 不着色） */
export function signClass(val, digits = 2) {
  const s = signOf(val, digits)
  return s > 0 ? 'up' : s < 0 ? 'down' : ''
}
