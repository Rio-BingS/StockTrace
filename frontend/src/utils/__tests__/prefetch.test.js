import { test } from 'node:test'
import assert from 'node:assert/strict'
import { prefetchWhenIdle, shouldPrefetch } from '../prefetch.js'

test('shouldPrefetch：省流量与 2G 下不预取（FEX-07）', () => {
  assert.equal(shouldPrefetch({}), true)
  assert.equal(shouldPrefetch({ connection: { saveData: true } }), false)
  assert.equal(shouldPrefetch({ connection: { effectiveType: 'slow-2g' } }), false)
  assert.equal(shouldPrefetch({ connection: { effectiveType: '4g' } }), true)
})

test('prefetchWhenIdle：空闲回调里执行加载器，失败不抛', async () => {
  let idle
  const win = { requestIdleCallback: (fn) => { idle = fn } }
  const calls = []
  const ok = prefetchWhenIdle([
    () => { calls.push('a'); return Promise.resolve() },
    () => { calls.push('b'); return Promise.reject(new Error('offline')) },
  ], { win, nav: {} })
  assert.equal(ok, true)
  assert.deepEqual(calls, [])
  idle()
  await new Promise(r => setTimeout(r, 0))
  assert.deepEqual(calls, ['a', 'b'])
  assert.equal(prefetchWhenIdle([], { win: null }), false)
})
