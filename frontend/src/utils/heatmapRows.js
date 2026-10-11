/** 热力图窄屏只显示前 N 行（后端已按最大 |分值| 降序），避免手机上图表高达上千像素（FEX-05） */
export const NARROW_TOP_N = 15

export function visibleRows(rows, { narrow, expanded, topN = NARROW_TOP_N }) {
  const list = rows || []
  if (!narrow || expanded || list.length <= topN) return list
  return list.slice(0, topN)
}

/** 图表高度：每行 26px 加坐标轴与色条留白，最小 300 */
export function heatmapHeight(rowCount) {
  return Math.max(300, rowCount * 26 + 100)
}
