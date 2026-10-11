import { test } from 'node:test'
import assert from 'node:assert/strict'
import { extraColumnsFor, fmtCell } from '../screenerColumns.js'

test('extraColumnsFor：条件与排序用到、固定列外的指标去重（ARC-09）', () => {
  const cols = extraColumnsFor({
    conditions: [
      { field: 'change_pct' },
      { field: 'pct_5d' },
      { field: 'above_ma5' },
      { field: 'pct_5d' },
    ],
    order_by: 'volume',
  })
  assert.deepEqual(
    cols.map((c) => c.key),
    ['pct_5d', 'above_ma5', 'volume'],
  )
  assert.equal(cols[1].bool, true)
  assert.equal(cols[0].label, '5日涨跌幅(%)')
  assert.deepEqual(extraColumnsFor(null), [])
})

test('fmtCell：按字段类型格式化', () => {
  const row = {
    pct_5d: 1.234,
    above_ma5: 1,
    volume: 1_234_567,
    up_days: 3,
    turnover_5d_avg: 2.5e8,
    x: null,
  }
  assert.equal(fmtCell(row, { key: 'pct_5d' }), '+1.23%')
  assert.equal(fmtCell(row, { key: 'above_ma5', bool: true }), '是')
  assert.equal(fmtCell(row, { key: 'volume' }), '123.46 万股')
  assert.equal(fmtCell(row, { key: 'up_days' }), '3')
  assert.equal(fmtCell(row, { key: 'turnover_5d_avg' }), '2.50 亿')
  assert.equal(fmtCell(row, { key: 'x' }), '-')
})

test('fmtCell：换手率不加正号，市值换算万/亿（FEF-11）', () => {
  const row = { turnover_rate: 3.2, float_mv: 1.23e10, total_mv: 5e11 }
  assert.equal(fmtCell(row, { key: 'turnover_rate' }), '3.20%')
  assert.equal(fmtCell(row, { key: 'float_mv' }), '123.00 亿')
  assert.equal(fmtCell(row, { key: 'total_mv' }), '5000.00 亿')
})
