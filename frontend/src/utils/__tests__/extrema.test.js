import { test } from 'node:test'
import assert from 'node:assert/strict'
import { findLocalExtrema } from '../extrema.js'

// 构造 W 形走势：10 → 9（谷）→ 11（峰）→ 8.5（谷）→ 10
function wave() {
  const pts = []
  const seg = (a, b, n) => { for (let i = 0; i < n; i++) pts.push(+(a + (b - a) * i / n).toFixed(3)) }
  seg(10, 9, 30); seg(9, 11, 30); seg(11, 8.5, 30); seg(8.5, 10, 30)
  return pts
}

test('findLocalExtrema：识别阶段峰谷（ARC-09）', () => {
  const prices = wave()
  const { peaks, valleys } = findLocalExtrema(prices)
  assert.deepEqual(peaks.map(p => p.price), [11])
  assert.deepEqual(valleys.map(v => v.price), [9, 8.5])
  assert.equal(prices[peaks[0].index], 11)
})

test('findLocalExtrema：数据不足 2×窗口+1 时不检测', () => {
  assert.deepEqual(findLocalExtrema([1, 2, 3]), { peaks: [], valleys: [] })
})
