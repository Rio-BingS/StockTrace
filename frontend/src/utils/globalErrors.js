/**
 * 全局错误兜底（FEX-06）：组件渲染/生命周期抛错、未处理的 Promise 拒绝，
 * 以前只在控制台可见，页面静默失效；现在统一提示一次。主动取消的请求不提示。
 */
import { errorMessage, isCanceled } from './errorMessage.js'

/** 返回要提示给用户的文案；不需要提示时返回 '' */
export function globalErrorText(err) {
  if (!err || isCanceled(err)) return ''
  // 接口错误（带 response / request）沿用统一文案；其它为前端程序错误
  if (err.response || err.request || err.isAxiosError) return errorMessage(err, '请求失败')
  return '页面出现异常，部分内容可能未正确显示，请刷新重试'
}

export function installGlobalErrorHandlers(app, notify) {
  app.config.errorHandler = (err, instance, info) => {
    console.error('[vue error]', info, err)
    const text = globalErrorText(err)
    if (text) notify(text)
  }
  window.addEventListener('unhandledrejection', (event) => {
    const text = globalErrorText(event.reason)
    if (!text) return
    console.error('[unhandled rejection]', event.reason)
    notify(text)
  })
}
