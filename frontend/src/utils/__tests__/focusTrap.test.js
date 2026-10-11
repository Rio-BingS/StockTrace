import { test } from 'node:test'
import assert from 'node:assert/strict'
import { setInert, trapIndex } from '../focusTrap.js'

test('trapIndex：Tab 在对话框内首尾循环（FEX-04）', () => {
  assert.equal(trapIndex(0, 3, false), 1)
  assert.equal(trapIndex(2, 3, false), 0)
  assert.equal(trapIndex(0, 3, true), 2)
  assert.equal(trapIndex(-1, 3, false), 0)
  assert.equal(trapIndex(-1, 3, true), 2)
  assert.equal(trapIndex(0, 0, false), -1)
})

test('setInert：设置与移除 inert / aria-hidden', () => {
  const attrs = new Map()
  const el = {
    setAttribute: (k, v) => attrs.set(k, v),
    removeAttribute: (k) => attrs.delete(k),
  }
  setInert(el, true)
  assert.ok(attrs.has('inert') && attrs.get('aria-hidden') === 'true')
  setInert(el, false)
  assert.equal(attrs.size, 0)
  setInert(null, true) // 不抛错
})
