import { test } from 'node:test'
import assert from 'node:assert/strict'
import { globalErrorText, installGlobalErrorHandlers } from '../globalErrors.js'

test('globalErrorText：取消的请求不提示，接口错误用统一文案，程序错误给通用提示（FEX-06）', () => {
  assert.equal(globalErrorText(null), '')
  assert.equal(globalErrorText({ name: 'CanceledError', code: 'ERR_CANCELED' }), '')
  const apiErr = { isAxiosError: true, response: { status: 502, data: '<html>bad gateway</html>' } }
  const t = globalErrorText(apiErr)
  assert.ok(t && !t.includes('<html>'))
  assert.match(globalErrorText(new TypeError('x is undefined')), /页面出现异常/)
})

test('installGlobalErrorHandlers：挂上 Vue errorHandler 与 unhandledrejection', () => {
  const listeners = {}
  globalThis.window = { addEventListener: (k, fn) => { listeners[k] = fn } }
  const origError = console.error
  console.error = () => {}
  try {
    const app = { config: {} }
    const seen = []
    installGlobalErrorHandlers(app, msg => seen.push(msg))
    app.config.errorHandler(new Error('boom'), null, 'render')
    listeners.unhandledrejection({ reason: { name: 'CanceledError', code: 'ERR_CANCELED' } })
    listeners.unhandledrejection({ reason: new Error('async boom') })
    assert.equal(seen.length, 2)
  } finally {
    console.error = origError
    delete globalThis.window
  }
})
