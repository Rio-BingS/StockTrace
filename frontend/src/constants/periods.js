/** 资金流向等区间选择器的选项（ARC-06），原来在三个页面各写一份 */
const BASE = [
  ['1d', '当日'],
  ['3d', '3日'],
  ['5d', '5日'],
  ['1w', '近1周'],
  ['1m', '近1月'],
  ['3m', '近3月'],
  ['6m', '近半年'],
]

/** 大盘资金流（市场概览） */
export const FLOW_PERIODS = [...BASE, ['ytd', '今年以来']]

/** 国家队 ETF 区间资金流向：支持自定义起止日期 */
export const FLOW_PERIODS_WITH_CUSTOM = [...FLOW_PERIODS, ['custom', '自定义']]

/** 北向资金区间：多一个近 1 年 */
export const NORTH_PERIODS = [...BASE, ['1y', '近1年'], ['ytd', '今年以来']]
