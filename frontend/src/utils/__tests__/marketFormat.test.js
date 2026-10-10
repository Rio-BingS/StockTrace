// 运行：npm test（Node 内置 node:test，无需额外依赖）
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { formatAmount, formatYiAxis } from '../marketFormat.js'

test('formatAmount：输入单位为元，换算到 亿（DAT-04 主力合计）', () => {
  // 东财 fflow 主力净流入（元）求和后的典型量级
  assert.equal(formatAmount(52345678900), '523.46 亿')
  assert.equal(formatAmount(-52345678900), '-523.46 亿')
  assert.equal(formatAmount(52345678900, { signed: true }), '+523.46 亿')
  assert.equal(formatAmount(1.5e12), '1.50 万亿')
  assert.equal(formatAmount(1234567), '123.46 万')
  assert.equal(formatAmount(9999.4), '9999')
})

test('formatAmount：单位边界四舍五入后进位', () => {
  assert.equal(formatAmount(99_999_999.9), '1.00 亿')
  assert.equal(formatAmount(99_999_949), '9999.99 万')
  assert.equal(formatAmount(9_999.6), '1.00 万')
  assert.equal(formatAmount(999_999_995_000), '1.00 万亿')
})

test('formatAmount：空值与零', () => {
  assert.equal(formatAmount(null), '-')
  assert.equal(formatAmount(undefined), '-')
  assert.equal(formatAmount(''), '-')
  assert.equal(formatAmount('abc'), '-')
  assert.equal(formatAmount(0, { signed: true }), '0')
})

test('formatYiAxis：坐标轴 元→亿 取整', () => {
  assert.equal(formatYiAxis(-5_234_567_890), '-52亿')
  assert.equal(formatYiAxis(1e7), '0亿')
  assert.equal(formatYiAxis(3e10), '300亿')
})
