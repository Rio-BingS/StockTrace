/**
 * 全站提示与确认框（FEX-06）：替代原生 alert / confirm / prompt。
 * 原生弹窗会阻塞页面、样式不可控、读屏体验差，且无法在测试中模拟。
 * - toast(message, { type, timeout })：右下角非阻塞提示，aria-live 播报
 * - confirmDialog({ title, message, confirmText, danger }) → Promise<boolean>
 * - promptDialog({ title, message, defaultValue, placeholder }) → Promise<string|null>（取消为 null）
 * 宿主组件 ToastHost / ConfirmDialog 挂在 App.vue。
 */
import { reactive } from 'vue'

let seq = 0

export const feedback = reactive({
  toasts: [],
  // 当前显示的对话框；同时只显示一个，其余排队
  dialog: null,
  queue: [],
})

export function dismissToast(id) {
  const i = feedback.toasts.findIndex(t => t.id === id)
  if (i >= 0) feedback.toasts.splice(i, 1)
}

/** 非阻塞提示；type: info | success | error。返回 id，可手动关闭 */
export function toast(message, { type = 'info', timeout = type === 'error' ? 6000 : 3000 } = {}) {
  if (!message) return null
  const id = ++seq
  feedback.toasts.push({ id, message: String(message), type })
  // 最多保留 4 条，最早的先移除
  while (feedback.toasts.length > 4) feedback.toasts.shift()
  if (timeout > 0) setTimeout(() => dismissToast(id), timeout)
  return id
}

export function toastError(message) {
  return toast(message, { type: 'error' })
}

function openDialog(spec) {
  return new Promise((resolve) => {
    const item = { id: ++seq, ...spec, resolve }
    if (feedback.dialog) feedback.queue.push(item)
    else feedback.dialog = item
  })
}

/** 关闭当前对话框并返回结果（ConfirmDialog 调用）；队列中的下一个随即显示 */
export function resolveDialog(result) {
  const d = feedback.dialog
  if (!d) return
  feedback.dialog = feedback.queue.shift() || null
  d.resolve(result)
}

export function confirmDialog({ title = '请确认', message = '', confirmText = '确定', cancelText = '取消', danger = false } = {}) {
  return openDialog({ kind: 'confirm', title, message, confirmText, cancelText, danger })
}

export function promptDialog({ title = '请输入', message = '', defaultValue = '', placeholder = '', confirmText = '确定', cancelText = '取消' } = {}) {
  return openDialog({ kind: 'prompt', title, message, defaultValue, placeholder, confirmText, cancelText })
}
