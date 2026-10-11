import { test } from 'node:test'
import assert from 'node:assert/strict'
import { escapeHtml } from '../escapeHtml.js'

test('escapeHtml：转义 HTML 特殊字符（SEC-09）', () => {
  assert.equal(
    escapeHtml('<img src=x onerror="alert(1)">'),
    '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;',
  )
  assert.equal(escapeHtml("A&B's"), 'A&amp;B&#39;s')
})

test('escapeHtml：空值返回空串，普通文本与数字原样', () => {
  assert.equal(escapeHtml(null), '')
  assert.equal(escapeHtml(undefined), '')
  assert.equal(escapeHtml('半导体'), '半导体')
  assert.equal(escapeHtml(512480), '512480')
})
