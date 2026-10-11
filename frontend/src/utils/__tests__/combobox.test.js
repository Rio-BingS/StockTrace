import { test } from 'node:test'
import assert from 'node:assert/strict'
import { moveHighlight, resolveEnter } from '../combobox.js'

const results = [
  { code: '000001', name: '平安银行' },
  { code: '601318', name: '中国平安' },
]

test('moveHighlight：上下移动并首尾循环（FEF-10）', () => {
  assert.equal(moveHighlight(-1, 1, 2), 0)
  assert.equal(moveHighlight(-1, -1, 2), 1)
  assert.equal(moveHighlight(0, 1, 2), 1)
  assert.equal(moveHighlight(1, 1, 2), 0)
  assert.equal(moveHighlight(0, -1, 2), 1)
  assert.equal(moveHighlight(0, 1, 0), -1)
})

test('resolveEnter：没有高亮时不再默认添加第一个结果', () => {
  assert.equal(resolveEnter({ keyword: '平安', results, highlighted: -1 }), null)
})

test('resolveEnter：只添加高亮项', () => {
  assert.deepEqual(resolveEnter({ keyword: '平安', results, highlighted: 1 }), {
    code: '601318',
    name: '中国平安',
  })
})

test('resolveEnter：完整 6 位代码可直接添加；不完整代码不添加', () => {
  assert.deepEqual(resolveEnter({ keyword: ' 600519 ', results: [], highlighted: -1 }), {
    code: '600519',
    name: '',
  })
  assert.equal(resolveEnter({ keyword: '60051', results: [], highlighted: -1 }), null)
  // 高亮越界（结果已刷新）时按输入框内容处理
  assert.equal(resolveEnter({ keyword: '平安', results, highlighted: 5 }), null)
})
