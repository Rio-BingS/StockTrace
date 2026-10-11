// 运行：npm test（Node 内置 node:test，无需额外依赖）
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { AI_DISCLAIMER_FALLBACK, aiDisclaimer } from '../aiRequest.js'

test('aiDisclaimer：优先使用后端返回的免责声明（AIX-04）', () => {
  assert.equal(aiDisclaimer({ disclaimer: '后端文本' }), '后端文本')
  assert.equal(aiDisclaimer({ disclaimer: null }), AI_DISCLAIMER_FALLBACK)
  assert.equal(aiDisclaimer({ disclaimer: { a: 1 } }), AI_DISCLAIMER_FALLBACK)
  assert.equal(aiDisclaimer(undefined), AI_DISCLAIMER_FALLBACK)
})
