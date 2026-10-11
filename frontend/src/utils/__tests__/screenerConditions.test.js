import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  buildConditions,
  conditionError,
  conditionErrors,
  hasDetail,
  newCondition,
} from '../screenerConditions.js'

const BOOL = new Set(['above_ma5'])

test('conditionError：空值/非数字/区间缺上限都判为不完整（FEF-09）', () => {
  assert.equal(conditionError({ field: 'change_pct', op: 'gt', value: '' }, BOOL), '请填写数值')
  assert.equal(conditionError({ field: 'change_pct', op: 'gt', value: 'abc' }, BOOL), '数值无效')
  assert.equal(
    conditionError({ field: 'change_pct', op: 'between', value: '1' }, BOOL),
    '请填写上限',
  )
  assert.equal(
    conditionError({ field: 'change_pct', op: 'between', value: '5', value2: '1' }, BOOL),
    '下限不能大于上限',
  )
  assert.equal(
    conditionError({ field: 'change_pct', op: 'between', value: '1', value2: '5' }, BOOL),
    '',
  )
  assert.equal(conditionError({ field: 'change_pct', op: 'gt', value: 0 }, BOOL), '')
  assert.equal(conditionError({ field: 'change_pct', op: 'gt', value: '-3.5' }, BOOL), '')
})

test('conditionError：布尔字段只接受 1/0', () => {
  assert.equal(conditionError({ field: 'above_ma5', op: 'eq', value: '1' }, BOOL), '')
  assert.equal(conditionError({ field: 'above_ma5', op: 'eq', value: 0 }, BOOL), '')
  assert.equal(conditionError({ field: 'above_ma5', op: 'eq', value: '' }, BOOL), '请选择是/否')
})

test('buildConditions：任一行不完整返回 null，不再静默丢弃', () => {
  const conds = [
    { field: 'change_pct', op: 'gt', value: '3' },
    { field: 'pe_ttm', op: 'lt', value: '' },
  ]
  assert.deepEqual(conditionErrors(conds, BOOL), ['', '请填写数值'])
  assert.equal(buildConditions(conds, BOOL), null)
  assert.equal(buildConditions([], BOOL), null)
})

test('buildConditions：全部有效时按后端格式序列化', () => {
  assert.deepEqual(
    buildConditions(
      [
        { field: 'change_pct', op: 'gt', value: '3' },
        { field: 'pe_ttm', op: 'between', value: '5', value2: '20' },
        { field: 'above_ma5', op: 'eq', value: '1' },
      ],
      BOOL,
    ),
    [
      { field: 'change_pct', op: 'gt', value: 3 },
      { field: 'pe_ttm', op: 'between', value: [5, 20] },
      { field: 'above_ma5', op: 'eq', value: 1 },
    ],
  )
})

test('hasDetail：stock_id 为空时不能跳转详情', () => {
  assert.equal(hasDetail({ stock_id: 12 }), true)
  assert.equal(hasDetail({ stock_id: 0 }), true)
  assert.equal(hasDetail({ stock_id: null }), false)
  assert.equal(hasDetail({ stock_id: '' }), false)
  assert.equal(hasDetail({}), false)
})

test('newCondition：每行有唯一 uid，且不进入提交给后端的 spec（FEF-17）', () => {
  const a = newCondition()
  const b = newCondition({ field: 'pct_5d', op: 'lt', value: '3' })
  assert.notEqual(a.uid, b.uid)
  assert.equal(b.field, 'pct_5d')
  assert.deepEqual(buildConditions([b], new Set()), [{ field: 'pct_5d', op: 'lt', value: 3 }])
})
