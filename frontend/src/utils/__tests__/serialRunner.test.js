import { test, mock } from 'node:test'
import assert from 'node:assert/strict'
import { createBurstScheduler, createSerialRunner } from '../serialRunner.js'

const flush = async () => {
  for (let i = 0; i < 10; i += 1) await Promise.resolve()
}

test('createSerialRunner：执行中的多次调用合并为结束后补跑一次（FEF-05）', async () => {
  const resolvers = []
  let calls = 0
  const run = createSerialRunner(() => {
    calls += 1
    return new Promise((r) => resolvers.push(r))
  })
  const p1 = run()
  const p2 = run()
  const p3 = run()
  assert.equal(calls, 1)
  assert.equal(p2, p3)
  resolvers[0]()
  await p1
  await flush()
  assert.equal(calls, 2)
  resolvers[1]()
  await p2
  assert.equal(calls, 2)
  // 空闲后再调用立即执行
  run()
  assert.equal(calls, 3)
  resolvers[2]()
})

test('createSerialRunner：上一次失败也会补跑，失败向调用方抛出', async () => {
  let calls = 0
  const run = createSerialRunner(async () => {
    calls += 1
    if (calls === 1) throw new Error('x')
  })
  const p1 = run()
  const p2 = run()
  await assert.rejects(p1)
  await p2
  assert.equal(calls, 2)
})

test('createBurstScheduler：按 3/8/15s 依次补刷，重复触发不叠加', async (t) => {
  mock.timers.enable({ apis: ['setTimeout'] })
  t.after(() => mock.timers.reset())
  let calls = 0
  const burst = createBurstScheduler(async () => {
    calls += 1
  })
  burst.trigger()
  mock.timers.tick(2000)
  await flush()
  burst.trigger() // 2s 时再次触发：从头重排，不应出现两套定时器
  mock.timers.tick(3000)
  await flush()
  assert.equal(calls, 1)
  mock.timers.tick(5000)
  await flush()
  assert.equal(calls, 2)
  mock.timers.tick(7000)
  await flush()
  assert.equal(calls, 3)
  mock.timers.tick(60000)
  await flush()
  assert.equal(calls, 3)
  assert.equal(burst.isScheduled(), false)
})

test('createBurstScheduler：cancel 后不再执行（卸载清理）', async (t) => {
  mock.timers.enable({ apis: ['setTimeout'] })
  t.after(() => mock.timers.reset())
  let calls = 0
  const burst = createBurstScheduler(async () => {
    calls += 1
  })
  burst.trigger()
  mock.timers.tick(3000)
  await flush()
  burst.cancel()
  mock.timers.tick(20000)
  await flush()
  assert.equal(calls, 1)
})
