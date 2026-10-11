// 运行：npm test（Node 内置 node:test，无需额外依赖）
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  AI_DISCLAIMER_FALLBACK,
  aiDisclaimer,
  aiErrorText,
  createAiTask,
  isCanceled,
} from '../aiRequest.js'

test('aiDisclaimer：优先使用后端返回的免责声明（AIX-04）', () => {
  assert.equal(aiDisclaimer({ disclaimer: '后端文本' }), '后端文本')
  assert.equal(aiDisclaimer({ disclaimer: null }), AI_DISCLAIMER_FALLBACK)
  assert.equal(aiDisclaimer({ disclaimer: { a: 1 } }), AI_DISCLAIMER_FALLBACK)
  assert.equal(aiDisclaimer(undefined), AI_DISCLAIMER_FALLBACK)
})

test('aiErrorText：只返回字符串，不会显示 [object Object]（AIX-05）', () => {
  assert.equal(aiErrorText({ response: { status: 502, data: { detail: '服务商返回错误（HTTP 500）' } } }),
    '服务商返回错误（HTTP 500）')
  assert.equal(aiErrorText({ response: { status: 400, data: { query: ['太长'] } } }), '请求参数有误')
  assert.equal(aiErrorText({ response: { status: 429, data: { detail: { x: 1 } } } }),
    '请求过于频繁或已有 AI 请求在进行，请稍后再试')
  assert.equal(aiErrorText({ response: { status: 502, data: '<html><body>Bad Gateway</body></html>' } }),
    'AI 服务暂不可用，请稍后再试')
  assert.equal(aiErrorText({ code: 'ECONNABORTED', message: 'timeout of 120000ms exceeded' }),
    '等待 AI 响应超时，请稍后再试')
  assert.equal(aiErrorText({ userMessage: '拦截器文案' }), '拦截器文案')
  for (const e of [null, {}, { response: { status: 500, data: null } }, { message: 'x' }]) {
    assert.equal(typeof aiErrorText(e), 'string')
  }
})

test('isCanceled：识别 axios 与原生取消', () => {
  assert.ok(isCanceled({ code: 'ERR_CANCELED' }))
  assert.ok(isCanceled({ name: 'AbortError' }))
  assert.ok(!isCanceled({ code: 'ECONNABORTED' }))
})

test('createAiTask：新请求取消旧请求，旧响应失效', () => {
  const task = createAiTask()
  const a = task.start()
  assert.ok(a.isCurrent())
  const b = task.start()
  assert.ok(a.signal.aborted)
  assert.ok(!a.isCurrent())
  assert.ok(b.isCurrent())
  task.cancel()
  assert.ok(b.signal.aborted)
  assert.ok(!b.isCurrent())
})
