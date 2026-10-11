import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  baseText,
  directionalExcess,
  fmtChanges,
  fmtExcessRate,
  fmtRate,
  fmtReturn,
  runReviewCells,
  signClass,
} from '../detectorReview.js'

test('fmtRate / fmtExcessRate：比率与百分点，缺失为 -（#7）', () => {
  assert.equal(fmtRate(0.453), '45%')
  assert.equal(fmtRate(0), '0%')
  assert.equal(fmtRate(null), '-')
  assert.equal(fmtRate(undefined), '-')
  assert.equal(fmtExcessRate(0.12), '+12pp')
  assert.equal(fmtExcessRate(-0.054), '-5pp')
  assert.equal(fmtExcessRate(0), '0pp')
  assert.equal(fmtExcessRate(null), '-')
})

test('fmtReturn / signClass：收益带符号，红涨绿跌', () => {
  assert.equal(fmtReturn(1.234), '+1.23%')
  assert.equal(fmtReturn(-0.5), '-0.50%')
  assert.equal(fmtReturn(null), '-')
  assert.equal(signClass(0.1), 'up')
  assert.equal(signClass(-0.1), 'down')
  assert.equal(signClass(0), '')
  assert.equal(signClass(null), '')
})

test('runReviewCells：新记录四件套齐全', () => {
  const cells = runReviewCells({
    hit_rate: 0.5,
    baseline_rate: 0.21,
    excess_rate: 0.29,
    independent_samples: 3,
    reviewed_count: 8,
  })
  assert.deepEqual(cells, {
    hit: '50%',
    baseline: '21%',
    excess: '+29pp',
    excessClass: 'up',
    samples: '3',
    reviewed: 8,
  })
})

test('runReviewCells：旧扫描记录字段为空时不报错，显示 -', () => {
  const cells = runReviewCells({ hit_rate: 0.4, reviewed_count: 5 })
  assert.equal(cells.hit, '40%')
  assert.equal(cells.baseline, '-')
  assert.equal(cells.excess, '-')
  assert.equal(cells.samples, '-')
  assert.equal(runReviewCells(null).hit, '-')
  assert.equal(runReviewCells(undefined).reviewed, 0)
})

test('directionalExcess：向下信号的超额取反，缺失为 null', () => {
  assert.equal(directionalExcess({ direction: 'up', excess_return: 1.5 }), 1.5)
  assert.equal(directionalExcess({ direction: 'down', excess_return: 1.5 }), -1.5)
  assert.equal(directionalExcess({ direction: 'up', excess_return: null }), null)
  assert.equal(directionalExcess(null), null)
})

test('baseText：板块 T+1 收盘、个股/指数 T+1 开盘（#12 文案）', () => {
  const base = { stock: 'open_t1', index: 'open_t1', industry: 'close_t1', concept: 'close_t1' }
  assert.equal(baseText(base, 'stock'), '次日（T+1）开盘')
  assert.equal(baseText(base, 'industry'), '次日（T+1）收盘')
  assert.equal(baseText(undefined, 'stock'), '-')
})

test('fmtChanges：调参流水的参数变更', () => {
  assert.equal(
    fmtChanges({ weight: [1, 1.2], noise_pct: [0.8, 0.75] }),
    'weight 1→1.2、noise_pct 0.8→0.75',
  )
  assert.equal(fmtChanges({}), '-')
  assert.equal(fmtChanges(null), '-')
})
