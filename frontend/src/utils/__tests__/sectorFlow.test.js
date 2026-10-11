import { test } from 'node:test'
import assert from 'node:assert/strict'
import { signedAmount } from '../sectorFlow.js'

test('signedAmount：净流入带正号，0 与空值不带（FEF-11）', () => {
  assert.equal(signedAmount(1.23e9), '+12.30 亿')
  assert.equal(signedAmount(-1.23e9), '-12.30 亿')
  assert.equal(signedAmount(0), '0')
  assert.equal(signedAmount(null), '-')
})
