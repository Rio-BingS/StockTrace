/** 对话框焦点管理的纯逻辑部分（FEX-04） */

export const FOCUSABLE = [
  'a[href]', 'button:not([disabled])', 'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])', 'textarea:not([disabled])', '[tabindex]:not([tabindex="-1"])',
].join(',')

/**
 * Tab / Shift+Tab 在对话框内循环：返回下一个应聚焦的下标；
 * current 为 -1（焦点不在对话框内）时，Tab 到第一个、Shift+Tab 到最后一个。
 */
export function trapIndex(current, count, shift) {
  if (count <= 0) return -1
  if (current < 0) return shift ? count - 1 : 0
  return shift ? (current - 1 + count) % count : (current + 1) % count
}

/** 打开对话框时让背景不可交互（inert），返回恢复函数 */
export function setInert(el, on) {
  if (!el) return
  if (on) {
    el.setAttribute('inert', '')
    el.setAttribute('aria-hidden', 'true')
  } else {
    el.removeAttribute('inert')
    el.removeAttribute('aria-hidden')
  }
}
