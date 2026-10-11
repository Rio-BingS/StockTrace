import { test } from 'node:test'
import assert from 'node:assert/strict'
import { ariaSort, onActivateKey } from '../a11y.js'

test('ariaSort：只有当前排序列有方向（FEX-03）', () => {
  assert.equal(ariaSort('net', 'desc', 'net'), 'descending')
  assert.equal(ariaSort('net', 'asc', 'net'), 'ascending')
  assert.equal(ariaSort('net', 'desc', 'inflow'), 'none')
})

test('onActivateKey：Enter/空格触发并阻止默认滚动，子元素事件与其它键忽略', () => {
  let n = 0
  const el = {}
  const ev = (key, target = el) => ({
    key,
    target,
    currentTarget: el,
    prevented: false,
    preventDefault() {
      this.prevented = true
    },
  })
  const e1 = ev('Enter')
  onActivateKey(e1, () => n++)
  const e2 = ev(' ')
  onActivateKey(e2, () => n++)
  onActivateKey(ev('a'), () => n++)
  onActivateKey(ev('Enter', {}), () => n++)
  assert.equal(n, 2)
  assert.ok(e1.prevented && e2.prevented)
})
