/** 探测器信号的展示文案（ARC-06），列表页与详情页共用 */
export const SEVERITY_LABEL = { high: '高', medium: '中', low: '低' }
export const STATUS_LABEL = { watching: '观察中', active: '维持中', confirmed: '已兑现', missed: '未兑现' }

export function dirText(direction) {
  return direction === 'up' ? '↑' : direction === 'down' ? '↓' : '—'
}
