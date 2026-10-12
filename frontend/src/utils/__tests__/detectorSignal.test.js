import { test } from 'node:test'
import assert from 'node:assert/strict'
import { isUnverified, POSITIONING_TEXT, UNVERIFIED_TEXT } from '../detectorSignal.js'

test('isUnverified：只有向上信号标「未经验证」', () => {
  assert.equal(isUnverified({ direction: 'up' }), true)
  assert.equal(isUnverified({ direction: 'down' }), false)
  assert.equal(isUnverified(null), false)
  assert.equal(UNVERIFIED_TEXT, '未经验证')
})

test('POSITIONING_TEXT：定位为趋势延续，不再说预判转折', () => {
  assert.match(POSITIONING_TEXT, /^趋势延续信号/)
  assert.match(POSITIONING_TEXT, /不是转折预判/)
  assert.doesNotMatch(POSITIONING_TEXT, /早期迹象|新阶段/)
})
