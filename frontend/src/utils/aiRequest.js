// AI 请求的前端公共小工具（AIX-04/05）

// 免责声明以后端返回的 disclaimer 为准；仅在响应缺字段时兜底
export const AI_DISCLAIMER_FALLBACK = 'AI 生成内容，仅供参考，不构成任何投资建议'

export function aiDisclaimer(data) {
  const text = data?.disclaimer
  return typeof text === 'string' && text.trim() ? text : AI_DISCLAIMER_FALLBACK
}
