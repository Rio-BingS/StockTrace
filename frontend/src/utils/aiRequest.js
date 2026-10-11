// AI 请求的前端公共小工具（AIX-04/05）
import { isCanceled } from './errorMessage.js'

// 免责声明以后端返回的 disclaimer 为准；仅在响应缺字段时兜底
export const AI_DISCLAIMER_FALLBACK = 'AI 生成内容，仅供参考，不构成任何投资建议'

export function aiDisclaimer(data) {
  const text = data?.disclaimer
  return typeof text === 'string' && text.trim() ? text : AI_DISCLAIMER_FALLBACK
}

// 主动取消（AbortController.abort）的请求：调用方应静默丢弃，不提示错误；
// 判定与通用 errorMessage.js 共用一份，避免两处规则漂移
export { isCanceled }

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
 * 经 api 实例发出的请求已由拦截器用通用 errorMessage() 挂好 err.userMessage，
 * 这里优先用它；下面的解析只兜底未经拦截器的错误（保留 AI 场景专用文案）。
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

const FIELD_LABELS = { base_url: '接口地址', api_key: 'API Key', name: '名称', model: '模型' }

/**
 * DRF 字段校验错误 → 一行文字：优先 base_url、api_key，再其他字段；取不到返回 ''。
 * 例：{ base_url: ['接口地址必须以 https:// 开头'] } → '接口地址：接口地址必须以 https:// 开头'
 */
export function fieldErrorText(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return ''
  if (typeof data.detail === 'string' && data.detail.trim()) return data.detail
  const keys = ['base_url', 'api_key', ...Object.keys(data).filter(k => k !== 'base_url' && k !== 'api_key')]
  for (const key of keys) {
    const value = data[key]
    // DRF 字段错误是字符串数组；其他字段（如 status: 'failed'）不是错误文案
    const list = Array.isArray(value) ? value : (FIELD_LABELS[key] ? [value] : [])
    const msg = list.find(v => typeof v === 'string' && v.trim())
    if (!msg) continue
    if (key === 'non_field_errors') return msg
    return FIELD_LABELS[key] ? `${FIELD_LABELS[key]}：${msg}` : msg
  }
  return ''
}
