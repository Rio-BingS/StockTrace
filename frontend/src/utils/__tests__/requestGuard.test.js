import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createRequestGuard } from '../requestGuard.js'

test('createRequestGuard：新请求取消旧请求，旧响应判定为过期（FEF-06）', () => {
  const guard = createRequestGuard()
  const a = guard.next()
  const b = guard.next()
  assert.equal(a.signal.aborted, true)
  assert.equal(a.isLatest(), false)
  assert.equal(b.signal.aborted, false)
  assert.equal(b.isLatest(), true)
})

test('createRequestGuard：乱序返回时只采用最后一次请求的结果', async () => {
  const guard = createRequestGuard()
  let state = null
  const fake = (value, ms) => new Promise((r) => setTimeout(() => r(value), ms))
  async function load(value, ms) {
    const req = guard.next()
    const res = await fake(value, ms)
    if (req.isLatest()) state = res
  }
  await Promise.all([load('old', 30), load('new', 5)])
  assert.equal(state, 'new')
})

test('createRequestGuard：abort 后在途请求被取消且不再是最新', () => {
  const guard = createRequestGuard()
  const a = guard.next()
  guard.abort()
  assert.equal(a.signal.aborted, true)
  assert.equal(a.isLatest(), false)
})
