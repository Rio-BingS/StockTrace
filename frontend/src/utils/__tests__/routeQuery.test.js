import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  assignRefs,
  changedKeys,
  parseEtfRadarQuery,
  parseNationalEtfQuery,
  parseSectorsQuery,
} from '../routeQuery.js'

const ref = (v) => ({ value: v })

test('parseSectorsQuery：非法值回落默认，页码至少为 1（FEF-15）', () => {
  assert.deepEqual(parseSectorsQuery({}), {
    board: 'industry',
    period: 'day',
    q: '',
    sort: 'net',
    order: 'desc',
    page: 1,
  })
  assert.deepEqual(
    parseSectorsQuery({ board: 'concept', period: '5d', page: '3', order: 'asc', q: '芯片' }),
    {
      board: 'concept',
      period: '5d',
      q: '芯片',
      sort: 'net',
      order: 'asc',
      page: 3,
    },
  )
  assert.equal(parseSectorsQuery({ period: '99d', page: '-2' }).period, 'day')
  assert.equal(parseSectorsQuery({ page: 'abc' }).page, 1)
  assert.equal(parseSectorsQuery({ q: ['a', 'b'] }).q, 'a')
  // 多日周期只能按净额排序（DAT-15）
  assert.equal(parseSectorsQuery({ period: '5d', sort: 'change_pct' }).sort, 'net')
  assert.equal(parseSectorsQuery({ sort: 'change_pct' }).sort, 'change_pct')
})

test('parseEtfRadarQuery / parseNationalEtfQuery：选中代码来自 code', () => {
  const s = parseEtfRadarQuery({ code: '510300', rank: 'turnover' })
  assert.equal(s.selectedCode, '510300')
  assert.equal(s.rank, 'turnover')
  assert.equal(s.scope, 'equity_broad')
  assert.deepEqual(parseNationalEtfQuery({}), { selectedCode: '' })
})

test('changedKeys：自己写回的 query 不算变化，外部改动才触发', () => {
  const refs = { board: ref('industry'), page: ref(2) }
  assert.deepEqual(changedKeys(refs, { board: 'industry', page: 2 }), [])
  assert.deepEqual(changedKeys(refs, { board: 'concept', page: 2 }), ['board'])
  assignRefs(refs, { board: 'concept', page: 1 })
  assert.equal(refs.board.value, 'concept')
  assert.equal(refs.page.value, 1)
})
