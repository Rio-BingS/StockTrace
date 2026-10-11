import { test } from 'node:test'
import assert from 'node:assert/strict'
import { heatmapHeight, visibleRows } from '../heatmapRows.js'

const rows = Array.from({ length: 40 }, (_, i) => ({ name: `板块${i}` }))

test('visibleRows：窄屏默认只显示前 15 行，展开后全部（FEX-05）', () => {
  assert.equal(visibleRows(rows, { narrow: true, expanded: false }).length, 15)
  assert.equal(visibleRows(rows, { narrow: true, expanded: true }).length, 40)
  assert.equal(visibleRows(rows, { narrow: false, expanded: false }).length, 40)
  assert.equal(visibleRows(rows.slice(0, 5), { narrow: true, expanded: false }).length, 5)
  assert.deepEqual(visibleRows(null, { narrow: true }), [])
})

test('heatmapHeight：最少 300px', () => {
  assert.equal(heatmapHeight(2), 300)
  assert.equal(heatmapHeight(40), 1140)
  assert.equal(heatmapHeight(15), 490)
})
