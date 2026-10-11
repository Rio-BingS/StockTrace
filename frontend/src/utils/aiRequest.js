// AI 请求的前端公共小工具（AIX-04/05）

// 免责声明以后端返回的 disclaimer 为准；仅在响应缺字段时兜底
export const AI_DISCLAIMER_FALLBACK = 'AI 生成内容，仅供参考，不构成任何投资建议'

export function aiDisclaimer(data) {
  const text = data?.disclaimer
  return typeof text === 'string' && text.trim() ? text : AI_DISCLAIMER_FALLBACK
}

// 主动取消（AbortController.abort）的请求：调用方应静默丢弃，不提示错误
export function isCanceled(err) {
  return !!err && (err.code === 'ERR_CANCELED' || err.name === 'CanceledError' || err.name === 'AbortError')
}

const AI_STATUS_TEXT = {
  400: '请求参数有误',
  413: '提交的内容过大',
  429: '请求过于频繁或已有 AI 请求在进行，请稍后再试',
  500: '服务器内部错误',
  502: 'AI 服务暂不可用，请稍后再试',
  503: '服务暂不可用，请稍后再试',
  504: '服务响应超时，请稍后再试',
}

/**
 * AI 请求错误 → 一行可展示的文字（AIX-05）；永远返回字符串，不会出现 [object Object]。
 * 合并后可替换为前端通用的 errorMessage()；先认拦截器挂好的 err.userMessage。
 */
export function aiErrorText(err) {
  if (!err) return '请求失败'
  if (isCanceled(err)) return '已取消'
  if (typeof err.userMessage === 'string' && err.userMessage) return err.userMessage
  const res = err.response
  if (res) {
    const data = res.data
    if (data && typeof data.detail === 'string' && data.detail.trim()) return data.detail.trim()
    if (typeof data === 'string' && data.trim() && !/<\/?(html|body|head)\b/i.test(data)) {
      const t = data.trim()
      return t.length > 200 ? t.slice(0, 200) + '…' : t
    }
    return AI_STATUS_TEXT[res.status] || `请求失败（HTTP ${res.status}）`
  }
  if (err.code === 'ECONNABORTED' || /timeout/i.test(err.message || '')) return '等待 AI 响应超时，请稍后再试'
  return '网络异常，请检查连接后重试'
}

/**
 * 单个 AI 请求槽位：新请求会取消旧请求，并让旧请求的迟到响应失效（AIX-05）。
 *   const task = createAiTask()
 *   const { signal, isCurrent } = task.start()
 *   const res = await api(..., { signal }); if (!isCurrent()) return
 */
export function createAiTask() {
  let controller = null
  let seq = 0
  return {
    start() {
      this.cancel()
      controller = new AbortController()
      const id = seq
      return { signal: controller.signal, isCurrent: () => id === seq }
    },
    cancel() {
      seq += 1
      if (controller) {
        controller.abort()
        controller = null
      }
    },
  }
}
