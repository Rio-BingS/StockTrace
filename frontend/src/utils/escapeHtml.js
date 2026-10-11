/**
 * HTML 转义：ECharts 自定义 tooltip formatter 返回的是 HTML 字符串，
 * 板块名、ETF 名称等来自第三方数据源，拼进去之前必须先转义（SEC-09）。
 * null/undefined 返回空串，其它值先转成字符串。
 */
const HTML_ESCAPES = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}

export function escapeHtml(val) {
  if (val == null) return ''
  return String(val).replace(/[&<>"']/g, ch => HTML_ESCAPES[ch])
}
