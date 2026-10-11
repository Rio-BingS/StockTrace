import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { FOCUSABLE, setInert, trapIndex } from '../utils/focusTrap.js'

// 打开中的对话框栈：叠加打开时（如设置弹窗上再弹确认框）只有栈顶响应键盘，
// 全部关闭后才解除背景 inert
const stack = []

/**
 * 模态对话框的焦点管理（FEX-04）：
 * - 打开时记住触发元素，聚焦 initialFocus（没有则第一个可聚焦元素）
 * - 背景 #app 设为 inert，读屏与 Tab 都进不去；对话框需 Teleport 到 body
 * - Tab 在对话框内循环，Esc 调用 onEscape
 * - 关闭后把焦点还给触发元素
 * open：控制显示的 ref；返回 { dialogRef, initialFocusRef } 绑到模板上
 */
export function useDialog(open, { onEscape } = {}) {
  const dialogRef = ref(null)
  const initialFocusRef = ref(null)
  let opener = null
  let active = false
  const token = {}

  function focusables() {
    return dialogRef.value ? [...dialogRef.value.querySelectorAll(FOCUSABLE)] : []
  }

  function onKeydown(e) {
    if (stack[stack.length - 1] !== token) return
    if (e.key === 'Escape') {
      if (onEscape) {
        e.preventDefault()
        onEscape()
      }
      return
    }
    if (e.key !== 'Tab') return
    const items = focusables()
    if (!items.length) return
    e.preventDefault()
    const next = trapIndex(items.indexOf(document.activeElement), items.length, e.shiftKey)
    items[next]?.focus()
  }

  async function activate() {
    if (active) return
    active = true
    opener = document.activeElement
    stack.push(token)
    setInert(document.getElementById('app'), true)
    document.addEventListener('keydown', onKeydown)
    await nextTick()
    const target = initialFocusRef.value || focusables()[0] || dialogRef.value
    target?.focus()
  }

  function deactivate() {
    if (!active) return
    active = false
    const i = stack.indexOf(token)
    if (i >= 0) stack.splice(i, 1)
    document.removeEventListener('keydown', onKeydown)
    if (!stack.length) setInert(document.getElementById('app'), false)
    // 触发元素可能已被移除（如删除行），存在才还焦点
    if (opener && document.contains(opener)) opener.focus()
    opener = null
  }

  watch(open, (v) => (v ? activate() : deactivate()), { immediate: true })
  onBeforeUnmount(deactivate)

  return { dialogRef, initialFocusRef }
}
