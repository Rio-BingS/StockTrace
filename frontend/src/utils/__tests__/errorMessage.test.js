import { test } from 'node:test'
import assert from 'node:assert/strict'
import { errorMessage, isCanceled } from '../errorMessage.js'

const resp = (status, data) => ({ response: { status, data } })

test('errorMessage：DRF detail 字符串原样展示（FEF-07）', () => {
  assert.equal(errorMessage(resp(400, { detail: '代码格式错误' })), '代码格式错误')
})

test('errorMessage：字段校验对象拍平，不显示 [object Object]', () => {
  const msg = errorMessage(resp(400, { code: ['该字段是必填项。'], name: ['太长'] }))
  assert.equal(msg, 'code：该字段是必填项。；name：太长')
  assert.ok(!msg.includes('[object Object]'))
})

test('errorMessage：non_field_errors 数组', () => {
  assert.equal(errorMessage(resp(400, { non_field_errors: ['a', 'b'] })), 'a；b')
})

test('errorMessage：nginx HTML 错误页不回显 HTML，按状态码给文案', () => {
  const html = '<html>\r\n<head><title>502 Bad Gateway</title></head>\r\n<body></body></html>'
  assert.equal(
    errorMessage(resp(502, html), '加载走势失败'),
    '加载走势失败：服务暂不可用（网关错误），请稍后再试',
  )
  assert.equal(
    errorMessage(resp(504, '<!DOCTYPE html><html></html>')),
    '请求失败：服务响应超时（网关超时），请稍后再试',
  )
  assert.ok(!errorMessage(resp(502, html)).includes('<'))
})

test('errorMessage：429 / 未知状态码 / 超时 / 断网', () => {
  assert.equal(errorMessage(resp(429, ''), '加载失败'), '加载失败：请求过于频繁，请稍后再试')
  assert.equal(errorMessage(resp(418, null), '加载失败'), '加载失败：HTTP 418')
  assert.match(
    errorMessage({ code: 'ECONNABORTED', message: 'timeout of 15000ms exceeded' }),
    /超时/,
  )
  assert.match(errorMessage({ code: 'ERR_NETWORK', message: 'Network Error' }), /网络/)
})

test('errorMessage：按调用方场景文案重新生成（不被拦截器的通用 userMessage 覆盖）；空值用兜底', () => {
  assert.equal(
    errorMessage({ userMessage: 'x', response: { status: 500 } }, '加载失败'),
    '加载失败：服务器内部错误',
  )
  assert.equal(errorMessage(null, '兜底'), '兜底')
  assert.equal(errorMessage(new Error('boom'), '兜底'), '兜底')
})

test('errorMessage：超长纯文本截断', () => {
  const msg = errorMessage(resp(500, 'x'.repeat(500)))
  assert.ok(msg.length <= 201)
})

test('isCanceled：识别 axios 取消与 AbortError', () => {
  assert.ok(isCanceled({ code: 'ERR_CANCELED' }))
  assert.ok(isCanceled({ name: 'AbortError' }))
  assert.ok(!isCanceled({ code: 'ECONNABORTED' }))
})
