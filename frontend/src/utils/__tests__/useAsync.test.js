import { test } from 'node:test'
import assert from 'node:assert/strict'
import { useAsync } from '../../composables/useAsync.js'
import { FLOW_PERIODS, FLOW_PERIODS_WITH_CUSTOM, NORTH_PERIODS } from '../../constants/periods.js'
import { dirText, SEVERITY_LABEL } from '../../constants/labels.js'
import { axisLabel, tooltip } from '../../charts/theme.js'

function deferred() {
  let resolve, reject
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

test('useAsync：成功写 data，失败给统一文案（ARC-06）', async () => {
  const ok = useAsync(async () => 42)
  await ok.run()
  assert.equal(ok.data.value, 42)
  assert.equal(ok.loading.value, false)

  const bad = useAsync(
    async () => {
      throw { response: { status: 500, data: '' } }
    },
    { errorText: '加载失败' },
  )
  await bad.run()
  assert.match(bad.error.value, /加载失败/)
  assert.equal(bad.data.value, null)
})

test('useAsync：新请求取消旧请求，旧响应晚到不覆盖', async () => {
  const calls = []
  const s = useAsync(({ signal }) => {
    const d = deferred()
    calls.push({ d, signal })
    return d.promise
  })
  const first = s.run()
  const second = s.run()
  assert.ok(calls[0].signal.aborted)
  calls[1].d.resolve('new')
  await second
  calls[0].d.resolve('old')
  await first
  assert.equal(s.data.value, 'new')
  assert.equal(s.loading.value, false)
})

test('区间选项与标签常量', () => {
  assert.equal(FLOW_PERIODS.at(-1)[0], 'ytd')
  assert.equal(FLOW_PERIODS_WITH_CUSTOM.at(-1)[0], 'custom')
  assert.deepEqual(NORTH_PERIODS.map((p) => p[0]).slice(-2), ['1y', 'ytd'])
  assert.equal(SEVERITY_LABEL.high, '高')
  assert.equal(dirText('down'), '↓')
  assert.equal(tooltip({ trigger: 'axis' }).trigger, 'axis')
  assert.equal(axisLabel({ fontSize: 10 }).fontSize, 10)
})
