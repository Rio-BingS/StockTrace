/**
 * 自选搜索组合框的键盘交互（FEF-10），纯函数，便于 node:test 单测。
 */

/** ↑/↓ 移动高亮：-1 表示无高亮；首尾循环 */
export function moveHighlight(index, delta, length) {
  if (!length) return -1
  if (index < 0) return delta > 0 ? 0 : length - 1
  return (index + delta + length) % length
}

/**
 * Enter 要添加的股票：只认高亮项，或输入框里是完整 6 位代码；
 * 否则返回 null（不再默认添加第一个搜索结果）。
 */
export function resolveEnter({ keyword, results, highlighted }) {
  const list = results || []
  if (highlighted >= 0 && highlighted < list.length) {
    const item = list[highlighted]
    return { code: item.code, name: item.name || '' }
  }
  const kw = (keyword || '').trim()
  if (/^\d{6}$/.test(kw)) return { code: kw, name: '' }
  return null
}
