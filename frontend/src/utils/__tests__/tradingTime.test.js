import { test } from 'node:test'
import assert from 'node:assert/strict'
import { beijingClock, isTradingTime, pollDelay } from '../tradingTime.js'

// 用带 +08:00 的 ISO 字符串构造，保证与运行环境时区无关
const bj = (s) => new Date(`${s}+08:00`)

test('beijingClock：按 UTC+8 换算星期与分钟（FEF-04）', () => {
  // 2026-10-12 是周一；UTC 周日 17:00 = 北京周一 01:00
  assert.deepEqual(beijingClock(new Date('2026-10-11T17:00:00Z')), { weekday: 1, minutes: 60 })
})

test('isTradingTime：上午盘 9:15–11:30 含两端', () => {
  assert.equal(isTradingTime(bj('2026-10-12T09:14:59')), false)
  assert.equal(isTradingTime(bj('2026-10-12T09:15:00')), true)
  assert.equal(isTradingTime(bj('2026-10-12T11:30:00')), true)
  assert.equal(isTradingTime(bj('2026-10-12T11:31:00')), false)
})

test('isTradingTime：午休不算、下午盘 13:00–15:00', () => {
  assert.equal(isTradingTime(bj('2026-10-12T12:00:00')), false)
  assert.equal(isTradingTime(bj('2026-10-12T12:59:00')), false)
  assert.equal(isTradingTime(bj('2026-10-12T13:00:00')), true)
  assert.equal(isTradingTime(bj('2026-10-16T15:00:00')), true)
  assert.equal(isTradingTime(bj('2026-10-16T15:01:00')), false)
})

test('isTradingTime：周末全天不是交易时段', () => {
  assert.equal(isTradingTime(bj('2026-10-10T10:00:00')), false) // 周六
  assert.equal(isTradingTime(bj('2026-10-11T14:00:00')), false) // 周日
})

test('pollDelay：交易时段用原间隔，非交易时段降到 10 分钟', () => {
  const opts = { interval: 60000 }
  assert.equal(pollDelay(bj('2026-10-12T10:00:00'), opts), 60000)
  assert.equal(pollDelay(bj('2026-10-12T20:00:00'), opts), 600000)
  assert.equal(pollDelay(bj('2026-10-12T20:00:00'), { interval: 60000, tradingOnly: false }), 60000)
  // 间隔本身比空闲间隔还长时不缩短
  assert.equal(pollDelay(bj('2026-10-12T20:00:00'), { interval: 900000 }), 900000)
})
