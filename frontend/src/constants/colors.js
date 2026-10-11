/**
 * 图表用色（ECharts 配置里不能直接写 CSS 变量），与 style.css 的 :root 变量保持一致（FEX-01）。
 * 红涨绿跌只用于涨跌；品牌强调色单独一个蓝色。
 */
export const UP = '#ff6b81'
export const DOWN = '#2ed47a'
export const FLAT = '#9aa3b2'
export const ACCENT = '#5b9bff'

/** 十六进制颜色加透明度："#ff6b81" + 0.5 → "rgba(255,107,129,0.5)" */
export function alpha(hex, a) {
  const h = hex.replace('#', '')
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16))
  return `rgba(${r},${g},${b},${a})`
}

/** 按数值正负取涨跌色；0 / 空值为平盘灰 */
export function trendColor(v) {
  const n = Number(v)
  if (v == null || v === '' || !Number.isFinite(n) || n === 0) return FLAT
  return n > 0 ? UP : DOWN
}
