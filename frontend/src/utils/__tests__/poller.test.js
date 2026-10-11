import { test, mock } from 'node:test'
import assert from 'node:assert/strict'
import { createPoller } from '../poller.js'

// 2026-10-12 周一 10:00 北京时间（交易时段）
const TRADING = new Date('2026-10-12T10:00:00+08:00').getTime()
// 2026-10-12 周一 20:00 北京时间（非交易时段）
const EVENING = new Date('2026-10-12T20:00:00+08:00').getTime()

function setup(startAt, opts = {}) {
  mock.timers.enable({ apis: ['setTimeout', 'Date'], now: startAt })
  let hidden = false
  let calls = 0
  const fn =
    opts.fn ||
    (() => {
      calls += 1
    })
  const poller = createPoller(fn, {
    interval: 60000,
    now: () => Date.now(),
    isHidden: () => hidden,
    ...opts,
  })
  return {
    poller,
    calls: () => calls,
    setHidden: (h) => {
      hidden = h
    },
  }
}

// 让 Promise 链跑完
const flush = async () => {
  for (let i = 0; i < 5; i += 1) await Promise.resolve()
}

test('poller：首次启动立即执行，交易时段按间隔串行轮询（FEF-04）', async (t) => {
  t.after(() => mock.timers.reset())
  const s = setup(TRADING)
  s.poller.start()
  await flush()
  assert.equal(s.calls(), 1)
  mock.timers.tick(59999)
  await flush()
  assert.equal(s.calls(), 1)
  mock.timers.tick(1)
  await flush()
  assert.equal(s.calls(), 2)
  s.poller.stop()
})

test('poller：非交易时段降到 10 分钟', async (t) => {
  t.after(() => mock.timers.reset())
  const s = setup(EVENING)
  s.poller.start()
  await flush()
  mock.timers.tick(60000)
  await flush()
  assert.equal(s.calls(), 1)
  mock.timers.tick(540000)
  await flush()
  assert.equal(s.calls(), 2)
  s.poller.stop()
})

test('poller：页面隐藏时暂停，恢复可见且已过间隔时立即补一次', async (t) => {
  t.after(() => mock.timers.reset())
  const s = setup(TRADING)
  s.poller.start()
  await flush()
  s.setHidden(true)
  s.poller.onVisibilityChange()
  assert.equal(s.poller.hasTimer(), false)
  mock.timers.tick(300000)
  await flush()
  assert.equal(s.calls(), 1)
  s.setHidden(false)
  s.poller.onVisibilityChange()
  await flush()
  assert.equal(s.calls(), 2)
  assert.equal(s.poller.hasTimer(), true)
  s.poller.stop()
})

test('poller：stop 后不再轮询；5 分钟内重新 start 不刷新，超过 5 分钟静默刷新', async (t) => {
  t.after(() => mock.timers.reset())
  const s = setup(TRADING)
  s.poller.start()
  await flush()
  s.poller.stop()
  mock.timers.tick(120000)
  await flush()
  assert.equal(s.calls(), 1)
  s.poller.start()
  await flush()
  assert.equal(s.calls(), 1) // 2 分钟前刚加载过
  s.poller.stop()
  mock.timers.tick(200000)
  await flush()
  s.poller.start()
  await flush()
  assert.equal(s.calls(), 2) // 已超过 5 分钟
  s.poller.stop()
})

test('poller：慢请求期间 refresh 复用同一请求，不叠加', async (t) => {
  t.after(() => mock.timers.reset())
  let resolve
  let calls = 0
  const s = setup(TRADING, {
    fn: () => {
      calls += 1
      return new Promise((r) => {
        resolve = r
      })
    },
  })
  s.poller.start()
  await flush()
  s.poller.refresh()
  s.poller.refresh()
  await flush()
  assert.equal(calls, 1)
  resolve()
  await flush()
  assert.equal(s.poller.hasTimer(), true)
  s.poller.stop()
})
