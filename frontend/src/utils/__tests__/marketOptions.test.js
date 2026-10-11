import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  buildActivityOption,
  buildFundFlowOption,
  mainNetInYuan,
} from '../../charts/marketOptions.js'
import { DOWN, FLAT, UP } from '../../constants/colors.js'

test('mainNetInYuan：北向兜底源由亿元换算为元（ARC-09 / DAT-04）', () => {
  const items = [{ main_net: 12.5 }, { main_net: null }]
  assert.deepEqual(mainNetInYuan({ source: 'eastmoney_hsgt_hist_north', items }), [12.5e8, null])
  assert.deepEqual(mainNetInYuan({ source: 'eastmoney_fflow', items }), [12.5, null])
  assert.deepEqual(mainNetInYuan(null), [])
})

test('buildFundFlowOption：柱色按正负，tooltip 带符号金额', () => {
  const opt = buildFundFlowOption({
    source: 'eastmoney_fflow',
    items: [
      { date: '2026-10-09T00:00:00', main_net: 5e9 },
      { date: '2026-10-10', main_net: -2e9 },
      { date: '2026-10-11', main_net: 0 },
    ],
  })
  assert.deepEqual(opt.xAxis.data, ['2026-10-09', '2026-10-10', '2026-10-11'])
  const color = opt.series[0].itemStyle.color
  assert.equal(color({ dataIndex: 0 }), UP)
  assert.equal(color({ dataIndex: 1 }), DOWN)
  assert.equal(color({ dataIndex: 2 }), FLAT)
  assert.equal(opt.tooltip.valueFormatter(5e9), '+50.00 亿')
  assert.deepEqual(buildFundFlowOption({ items: [] }), {})
})

test('buildActivityOption：无家数时为空配置', () => {
  assert.deepEqual(buildActivityOption({}), {})
  const opt = buildActivityOption({ up: 3000, down: 1500, flat: 200 })
  assert.deepEqual(
    opt.series[0].data.map((d) => d.value),
    [3000, 1500, 200],
  )
})
