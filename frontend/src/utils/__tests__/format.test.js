// 运行：npm test（Node 内置 node:test，无需额外依赖）
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { formatPct, formatRate, formatSigned, pctFrom, pctClass } from '../format.js'

test('pctFrom：涨跌幅以昨收为基准（FEF-01）', () => {
  // 高开低走但收盘仍高于昨收：较昨收为正，较开盘为负
  const prevClose = 10, open = 10.5, close = 10.4
  assert.ok(pctFrom(close, prevClose) > 0)
  assert.ok(pctFrom(close, open) < 0)
  assert.equal(formatPct(pctFrom(close, prevClose)), '+4.00%')
  assert.equal(pctClass(pctFrom(close, prevClose)), 'up')
})

test('pctFrom：基准缺失或为 0 时返回 null（显示 "-"，不回退开盘口径）', () => {
  assert.equal(pctFrom(10, null), null)
  assert.equal(pctFrom(10, 0), null)
  assert.equal(pctFrom(null, 10), null)
  assert.equal(formatPct(pctFrom(10, null)), '-')
})

test('formatSigned：涨跌额带正负号', () => {
  assert.equal(formatSigned(0.4), '+0.40')
  assert.equal(formatSigned('-0.25'), '-0.25')
  assert.equal(formatSigned(0), '0.00')
  assert.equal(formatSigned(null), '-')
})

test('formatPct：空串不显示为 0，±0 不带符号（FEF-11）', () => {
  assert.equal(formatPct(''), '-')
  assert.equal(formatPct(-0.004), '0.00%')
  assert.equal(formatPct(Infinity), '-')
  assert.equal(pctClass('+1.23%'), 'up')
  assert.equal(pctClass(-0.001), '')
})

test('formatRate：振幅不加正号（FEF-11）', () => {
  assert.equal(formatRate(3.5), '3.50%')
})
