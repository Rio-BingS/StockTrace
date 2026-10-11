import { test } from 'node:test'
import assert from 'node:assert/strict'
import { pageTitle, scrollBehavior } from '../routerHelpers.js'

test('pageTitle：带页面名与站点名（FEF-16）', () => {
  assert.equal(pageTitle('自选'), '自选 · StockTrace')
  assert.equal(pageTitle(undefined), 'StockTrace - A股监控')
})

test('scrollBehavior：后退恢复位置、同页改 query 不滚动、换页回顶部', () => {
  const saved = { left: 0, top: 480 }
  assert.equal(scrollBehavior({ path: '/market' }, { path: '/' }, saved), saved)
  assert.equal(
    scrollBehavior({ path: '/market/sectors', hash: '' }, { path: '/market/sectors' }, null),
    false,
  )
  assert.deepEqual(scrollBehavior({ path: '/stock/1', hash: '' }, { path: '/' }, null), { top: 0 })
  assert.deepEqual(scrollBehavior({ path: '/x', hash: '#a' }, { path: '/' }, null), { el: '#a' })
})
