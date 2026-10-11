import { test } from 'node:test'
import assert from 'node:assert/strict'
import { adjustHint, adjustLabel, priceSeriesName } from '../adjust.js'

test('adjustLabel：后端 history.adjust 映射为中文（DAT-14 前端标注）', () => {
  assert.equal(adjustLabel('qfq'), '前复权')
  assert.equal(adjustLabel('none'), '不复权')
  assert.equal(adjustLabel('hfq'), '后复权')
})

test('adjustLabel：缺失或未知取值返回空串，不猜测口径', () => {
  assert.equal(adjustLabel(undefined), '')
  assert.equal(adjustLabel(null), '')
  assert.equal(adjustLabel('QFQ'), '')
  assert.equal(adjustLabel('toString'), '')
  assert.equal(adjustHint('xxx'), '')
  assert.match(adjustHint('qfq'), /回溯调整/)
  assert.match(adjustHint('none'), /跳空/)
})

test('priceSeriesName：图例带口径，未知口径保持原名', () => {
  assert.equal(priceSeriesName('qfq'), '收盘价（前复权）')
  assert.equal(priceSeriesName('none'), '收盘价（不复权）')
  assert.equal(priceSeriesName(undefined), '收盘价')
  assert.equal(priceSeriesName('qfq', '价格'), '价格（前复权）')
})
