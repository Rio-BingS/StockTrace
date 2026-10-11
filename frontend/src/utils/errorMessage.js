/**
 * 统一的接口错误文案提取（FEF-07）。
 * 输入 axios 错误（或任意 Error），输出可直接展示给用户的中文文案：
 * - DRF detail 字符串 / 字段校验对象 / 非字段错误数组
 * - nginx / Cloudflare 返回的 HTML 网关错误页（不把整页 HTML 显示出来）
 * - 超时、网络断开、429 限流、5xx
 * 纯函数，不依赖 axios，便于 node:test 单测。
 */

const STATUS_TEXT = {
  400: '请求参数有误',
  401: '未登录或登录已过期',
  403: '没有权限执行该操作',
  404: '请求的资源不存在',
  409: '操作冲突，请稍后再试',
  413: '提交的内容过大',
  429: '请求过于频繁，请稍后再试',
  500: '服务器内部错误',
  502: '服务暂不可用（网关错误），请稍后再试',
  503: '服务暂不可用，请稍后再试',
  504: '服务响应超时（网关超时），请稍后再试',
}

const MAX_LEN = 200

function looksLikeHtml(text) {
  return /^\s*<(!doctype|html|head|body)\b/i.test(text) || /<\/(html|body)>/i.test(text)
}

function clip(text) {
  const t = String(text).trim()
  return t.length > MAX_LEN ? t.slice(0, MAX_LEN) + '…' : t
}

/** 把 DRF 风格的错误体拍平成一行文字；无法识别时返回 '' */
function flattenData(data) {
  if (data == null) return ''
  if (typeof data === 'string') {
    if (!data.trim() || looksLikeHtml(data)) return ''
    return clip(data)
  }
  if (Array.isArray(data)) {
    return clip(data.map(flattenData).filter(Boolean).join('；'))
  }
  if (typeof data === 'object') {
    for (const key of ['detail', 'message', 'error', 'non_field_errors']) {
      const text = flattenData(data[key])
      if (text) return text
    }
    // 字段校验错误：{ field: ['msg'] } → "field：msg"
    const parts = []
    for (const [key, val] of Object.entries(data)) {
      const text = flattenData(val)
      if (text) parts.push(`${key}：${text}`)
    }
    return clip(parts.join('；'))
  }
  return ''
}

/**
 * @param {unknown} err   axios 错误或任意异常
 * @param {string} fallback 调用方场景文案（如"加载走势失败"）
 * 后端给出明确错误文字时直接展示；否则返回"场景文案：原因"（原因取状态码/超时/断网）。
 */
export function errorMessage(err, fallback = '请求失败') {
  if (!err) return fallback
  // 主动取消的请求不应提示错误；调用方一般会先用 isCanceled() 过滤
  if (isCanceled(err)) return '请求已取消'

  let reason = ''
  const res = err.response
  if (res) {
    const text = flattenData(res.data)
    if (text) return text
    reason = STATUS_TEXT[res.status] || (res.status ? `HTTP ${res.status}` : '')
  } else if (err.code === 'ECONNABORTED' || err.code === 'ETIMEDOUT' || /timeout/i.test(err.message || '')) {
    reason = '请求超时，数据源可能较慢，请稍后重试'
  } else if (err.code === 'ERR_NETWORK' || err.message === 'Network Error') {
    reason = '网络连接失败，请检查网络'
  }
  return reason ? `${fallback}：${reason}` : fallback
}

/** 是否为主动取消（AbortController / 路由切走）导致的错误 */
export function isCanceled(err) {
  return !!err && (err.code === 'ERR_CANCELED' || err.name === 'CanceledError' || err.name === 'AbortError')
}
