import { test } from 'node:test'
import assert from 'node:assert/strict'
import { excludedLine, excludedSummary, pickScreenerMeta } from '../screenerMeta.js'

test('pickScreenerMeta：取后端 as_of/excluded（ALG-12 前端展示）', () => {
  const meta = pickScreenerMeta({
    count: 1,
    results: [],
    as_of: '2026-10-09',
    excluded: [
      {
        stock_id: 3,
        code: '600000',
        name: '浦发银行',
        last_trade_date: '2026-09-30',
        reason: '最新已收盘日线停在 2026-09-30',
      },
    ],
  })
  assert.equal(meta.asOf, '2026-10-09')
  assert.equal(meta.excluded.length, 1)
  assert.equal(meta.excluded[0].code, '600000')
})

test('pickScreenerMeta：旧后端缺字段或类型不对时给安全默认值', () => {
  assert.deepEqual(pickScreenerMeta({ count: 0, results: [] }), { asOf: null, excluded: [] })
  assert.deepEqual(pickScreenerMeta(null), { asOf: null, excluded: [] })
  assert.deepEqual(pickScreenerMeta({ as_of: '', excluded: 'x' }), { asOf: null, excluded: [] })
  assert.deepEqual(pickScreenerMeta({ as_of: null, excluded: [null, 1] }), {
    asOf: null,
    excluded: [],
  })
})

test('excludedLine：优先用后端 reason，缺失时按 last_trade_date 兜底', () => {
  assert.equal(
    excludedLine({ code: '000001', name: '平安银行', reason: '无已收盘日线' }),
    '000001 平安银行 —— 无已收盘日线',
  )
  assert.equal(
    excludedLine({ code: '000002', last_trade_date: '2026-09-30' }),
    '000002 —— 最新已收盘日线停在 2026-09-30',
  )
  assert.equal(excludedLine({}), '未知股票 —— 无已收盘日线')
})

test('excludedSummary：无排除时为空串，不展示折叠区', () => {
  assert.equal(excludedSummary([], '2026-10-09'), '')
  assert.equal(excludedSummary(undefined, null), '')
  assert.equal(
    excludedSummary([{}, {}], '2026-10-09'),
    '2 只自选股未参与本次筛选（没有 2026-10-09 的已收盘日线）',
  )
  assert.equal(excludedSummary([{}], null), '1 只自选股未参与本次筛选（暂无已收盘日线）')
})
