import { test } from 'node:test'
import assert from 'node:assert/strict'
import { computePnl, syncDrafts, validateHolding } from '../holding.js'

test('computePnl：字段齐全才计算（FEF-08）', () => {
  assert.equal(computePnl({ cost_price: '10', quantity: 100, close_price: '12.5' }), 250)
  assert.equal(computePnl({ cost_price: null, quantity: 100, close_price: 12 }), null)
  assert.equal(computePnl({ cost_price: 10, quantity: null, close_price: 12 }), null)
  assert.equal(computePnl({ cost_price: 10, quantity: 100, close_price: null }), null)
})

test('syncDrafts：未编辑的行跟随服务端更新，编辑中的行保留草稿', () => {
  let drafts = syncDrafts({}, [
    { code: 'A', cost_price: 10, quantity: 100 },
    { code: 'B', cost_price: null, quantity: null },
  ])
  assert.deepEqual(drafts.B, { cost_price: '', quantity: '', dirty: false })
  drafts = { ...drafts, A: { cost_price: '11', quantity: 100, dirty: true } }
  const next = syncDrafts(drafts, [
    { code: 'A', cost_price: 10, quantity: 100 },
    { code: 'B', cost_price: 8, quantity: 200 },
  ])
  assert.equal(next.A.cost_price, '11')
  assert.deepEqual(next.B, { cost_price: 8, quantity: 200, dirty: false })
})

test('syncDrafts：移除已不在列表中的行，且不修改入参', () => {
  const prev = {
    A: { cost_price: 1, quantity: 1, dirty: false },
    Z: { cost_price: 2, quantity: 2, dirty: true },
  }
  const next = syncDrafts(prev, [{ code: 'A', cost_price: 1, quantity: 1 }])
  assert.deepEqual(Object.keys(next), ['A'])
  assert.ok('Z' in prev)
})

test('validateHolding：数量必须为非负整数，成本价必须为正数', () => {
  assert.deepEqual(validateHolding({ cost_price: '10.5', quantity: '100' }), {
    ok: true,
    cost: 10.5,
    qty: 100,
  })
  assert.deepEqual(validateHolding({ cost_price: '', quantity: '' }), {
    ok: true,
    cost: null,
    qty: null,
  })
  assert.deepEqual(validateHolding({ cost_price: '10', quantity: '0' }), {
    ok: true,
    cost: 10,
    qty: 0,
  })
  assert.equal(validateHolding({ cost_price: '10', quantity: '1.5' }).ok, false)
  assert.equal(validateHolding({ cost_price: '10', quantity: '-1' }).ok, false)
  assert.equal(validateHolding({ cost_price: '10', quantity: 'abc' }).ok, false)
  assert.equal(validateHolding({ cost_price: '0', quantity: '1' }).ok, false)
  assert.equal(validateHolding({ cost_price: '-3', quantity: '1' }).ok, false)
})
