/**
 * 价格 / 涨跌幅展示（A 股）。实现统一在 number.js（FEF-11），这里保留原有函数名供各页使用。
 */
import { fmtFixed, fmtPct, fmtRate, fmtSigned, signClass, toNum } from './number.js'

/** 有方向的百分比（涨跌幅）：正数带 "+"，舍入为 0 时不带符号 */
export function formatPct(val, digits = 2) {
  return fmtPct(val, digits)
}

/** 非负比率（振幅、换手率）：不带正号，调用方不着色 */
export function formatRate(val, digits = 2) {
  return fmtRate(val, digits)
}

export function formatNum(val, digits = 2) {
  return fmtFixed(val, digits)
}

/** 带正负号的数字（涨跌额、盈亏等），空值显示 '-' */
export function formatSigned(val, digits = 2) {
  return fmtSigned(val, digits)
}

/**
 * 相对基准价的涨跌幅（%）。base 缺失或为 0 时返回 null，由调用方显示 '-'。
 * A 股"涨跌幅"口径：基准为昨收（prev_close），不是开盘价。
 */
export function pctFrom(price, base) {
  const p = toNum(price)
  const b = toNum(base)
  if (p == null || b == null || b === 0) return null
  return (p - b) / b * 100
}

/** 涨跌着色：按两位小数舍入后判断，兼容 "+1.23%" 这类已格式化的文本 */
export function pctClass(val) {
  if (val == null) return ''
  const v = typeof val === 'string' ? val.replace('%', '') : val
  return signClass(v)
}

export function formatDateTime(dt) {
  if (!dt) return '-'
  return String(dt).replace('T', ' ').slice(0, 19)
}
