/**
 * 图表公共样式（ARC-06）：tooltip 与坐标轴原来在十余个组件里各写一份，
 * 色值还有两三套。新图表从这里取，改主题只改一处。
 */
export const TOOLTIP_STYLE = {
  backgroundColor: '#101827',
  borderColor: '#33415f',
  textStyle: { color: '#dddddd', fontSize: 12 },
}

/** 坐标轴文字（与 --text-muted 一致，对比度 ≥4.5）与轴线、分割线颜色 */
export const AXIS_LABEL_COLOR = '#9aa3b2'
export const AXIS_LINE_COLOR = '#444c5e'
export const SPLIT_LINE_COLOR = '#1a2744'

export function tooltip(extra = {}) {
  return { ...TOOLTIP_STYLE, ...extra, textStyle: { ...TOOLTIP_STYLE.textStyle, ...(extra.textStyle || {}) } }
}

export function axisLabel(extra = {}) {
  return { color: AXIS_LABEL_COLOR, fontSize: 11, ...extra }
}

export const axisLine = { lineStyle: { color: AXIS_LINE_COLOR } }
export const splitLine = { lineStyle: { color: SPLIT_LINE_COLOR } }
