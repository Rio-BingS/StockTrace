/** 可访问性小工具（FEX-03） */

/** 排序表头的 aria-sort 值：当前排序列给 ascending/descending，其它列 none */
export function ariaSort(currentField, order, field) {
  if (currentField !== field) return 'none'
  return order === 'asc' ? 'ascending' : 'descending'
}

/**
 * 让非按钮元素（如可点击的表格行）响应 Enter / 空格，行为与点击一致。
 * 用法：@keydown="onActivateKey($event, () => select(row))"
 */
export function onActivateKey(event, handler) {
  if (event.target !== event.currentTarget) return // 行内按钮等自身可聚焦的子元素自己处理
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    handler()
  }
}
