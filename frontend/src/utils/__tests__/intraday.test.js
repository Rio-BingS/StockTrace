import { test } from 'node:test'
import assert from 'node:assert/strict'
import { intradayBase, quoteForDate, signedPctText } from '../intraday.js'

test('intradayBase：优先昨收，缺失时回退开盘并注明（FEF-13）', () => {
  assert.deepEqual(intradayBase({ prev_close: '10.00', open_price: '10.5' }), {
    price: 10,
    label: '较昨收',
    isPrevClose: true,
  })
  assert.deepEqual(intradayBase({ prev_close: null, open_price: '10.5' }), {
    price: 10.5,
    label: '较开盘',
    isPrevClose: false,
  })
  assert.deepEqual(intradayBase({ prev_close: '', open_price: '' }), {
    price: null,
    label: '',
    isPrevClose: false,
  })
  assert.equal(intradayBase(null).price, null)
})

test('signedPctText：低于基准时只有负号，不出现 "+-"', () => {
  assert.equal(signedPctText(9.877, 10), '-1.23%')
  assert.equal(signedPctText(10.123, 10), '+1.23%')
  assert.equal(signedPctText(10, 10), '0.00%')
  assert.equal(signedPctText(10, null), '')
})

test('quoteForDate：只取同一交易日的日线，不跨日借用昨收', () => {
  const rows = [{ trade_date: '2026-10-09', prev_close: 10 }]
  const latest = { trade_date: '2026-10-10', prev_close: 11 }
  assert.equal(quoteForDate(rows, latest, '2026-10-09'), rows[0])
  assert.equal(quoteForDate(rows, latest, '2026-10-10'), latest)
  assert.equal(quoteForDate(rows, latest, '2026-09-01'), null)
})
