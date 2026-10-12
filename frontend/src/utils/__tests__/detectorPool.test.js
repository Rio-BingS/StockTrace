import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  canAddToWatchlist,
  kindText,
  POOL_TITLE,
  poolStatusText,
  sortPoolSignals,
  SOURCE_OPTIONS,
  sourceText,
} from '../detectorPool.js'

test('SOURCE_OPTIONS 与后端 ?source= 取值一致', () => {
  assert.deepEqual(
    SOURCE_OPTIONS.map(([v]) => v),
    ['', 'watchlist', 'pool', 'market'],
  )
  assert.match(POOL_TITLE, /沪深300\+中证500/)
})

test('sourceText / kindText：成分池个股与自选股分开标识', () => {
  assert.equal(sourceText({ source: 'pool' }), '算法推送')
  assert.equal(sourceText({}), '')
  assert.equal(kindText({ target_kind: 'stock', source: 'pool' }), '成分池')
  assert.equal(kindText({ target_kind: 'stock', source: 'watchlist' }), '自选股')
  assert.equal(
    kindText({ target_kind: 'index', target_kind_display: '指数', source: 'market' }),
    '指数',
  )
  assert.equal(kindText({ target_kind: 'stock', target_kind_display: '个股' }), '个股')
  assert.equal(kindText(null), '')
})

test('sortPoolSignals：向下在前，同方向按严重度再按强度', () => {
  const rows = [
    { id: 1, direction: 'up', severity: 'high', score: 90 },
    { id: 2, direction: 'down', severity: 'low', score: 80 },
    { id: 3, direction: 'down', severity: 'high', score: 40 },
    { id: 4, direction: 'down', severity: 'high', score: 60 },
    { id: 5, direction: 'up', severity: 'low', score: 99 },
  ]
  assert.deepEqual(
    sortPoolSignals(rows).map((r) => r.id),
    [4, 3, 2, 1, 5],
  )
  // 不改原数组
  assert.equal(rows[0].id, 1)
  assert.deepEqual(sortPoolSignals(null), [])
})

test('canAddToWatchlist：只有未在自选的个股显示加自选', () => {
  assert.equal(canAddToWatchlist({ target_kind: 'stock', in_watchlist: false }), true)
  assert.equal(canAddToWatchlist({ target_kind: 'stock', in_watchlist: true }), false)
  // 旧接口没有 in_watchlist：不显示，避免重复添加
  assert.equal(canAddToWatchlist({ target_kind: 'stock' }), false)
  assert.equal(canAddToWatchlist({ target_kind: 'industry', in_watchlist: false }), false)
})

test('poolStatusText：pending / done / off 与旧数据', () => {
  assert.equal(poolStatusText(null), '')
  assert.equal(poolStatusText({ status: null }), '')
  assert.match(
    poolStatusText({ status: 'pending', fresh: 400, expected: 780 }),
    /400\/780.*自动补跑/,
  )
  const done = poolStatusText({
    status: 'done',
    fresh: 770,
    expected: 780,
    skipped_count: 2,
    missing_count: 10,
    signals_raw: 35,
    signals_pushed: 20,
  })
  assert.match(done, /^成分池已扫描：770\/780/)
  assert.match(done, /2 只序列不可用/)
  assert.match(done, /10 只当日缺数/)
  assert.match(done, /推送前 20 条/)
  assert.equal(poolStatusText({ status: 'done' }), '成分池已扫描')
  assert.match(poolStatusText({ status: 'off', reason: '成分表为空' }), /成分表为空/)
})
