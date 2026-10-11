import { test } from 'node:test'
import assert from 'node:assert/strict'
import { byKey } from '../sortBy.js'

test('byKey：带空格的日期时间按字符串排序，不依赖 Date 解析（FEF-17）', () => {
  const rows = [
    { datetime: '2026-10-09 13:01:00' },
    { datetime: '2026-10-09 09:31:00' },
    { datetime: '2026-10-09 11:30:00' },
  ]
  assert.deepEqual([...rows].sort(byKey('datetime')).map(r => r.datetime.slice(11, 16)), ['09:31', '11:30', '13:01'])
  assert.deepEqual(
    [{ d: '2026-10-10' }, { d: '2026-09-30' }].sort(byKey('d')).map(r => r.d),
    ['2026-09-30', '2026-10-10'],
  )
})
