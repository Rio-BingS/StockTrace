import { test } from 'node:test'
import assert from 'node:assert/strict'
import { isUnverified, UNVERIFIED_TEXT } from '../detectorSignal.js'

test('isUnverified：只有向上信号标「未经验证」', () => {
  assert.equal(isUnverified({ direction: 'up' }), true)
  assert.equal(isUnverified({ direction: 'down' }), false)
  assert.equal(isUnverified(null), false)
  assert.equal(UNVERIFIED_TEXT, '未经验证')
})
