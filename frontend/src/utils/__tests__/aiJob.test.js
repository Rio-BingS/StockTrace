// 运行：npm test（Node 内置 node:test，无需额外依赖）
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { isQueuedJob, pollAiJob } from '../aiJob.js'

function seq(...jobs) {
  let i = 0
  return async () => ({ data: jobs[Math.min(i++, jobs.length - 1)] })
}

test('pollAiJob：排队→运行→完成，返回 result 并回调状态', async () => {
  const seen = []
  const result = await pollAiJob(
    seq(
      { status: 'queued', queued_ahead: 1 },
      { status: 'running' },
      { status: 'done', result: { analysis: 'x' } },
    ),
    { intervalMs: 1, onStatus: (j) => seen.push(j.status) },
  )
  assert.deepEqual(result, { analysis: 'x' })
  assert.deepEqual(seen, ['queued', 'running'])
})

test('pollAiJob：失败时抛出与 axios 同形的错误', async () => {
  await assert.rejects(
    pollAiJob(seq({ status: 'failed', http_status: 502, result: { detail: '服务商返回错误' } }), {
      intervalMs: 1,
    }),
    (err) => err.response.status === 502 && err.response.data.detail === '服务商返回错误',
  )
})

test('pollAiJob：中止与超时', async () => {
  const ctrl = new AbortController()
  const p = pollAiJob(seq({ status: 'queued' }), { intervalMs: 50, signal: ctrl.signal })
  setTimeout(() => ctrl.abort(), 5)
  await assert.rejects(p, (err) => err.code === 'ERR_CANCELED')
  await assert.rejects(
    pollAiJob(seq({ status: 'running' }), { intervalMs: 5, timeoutMs: 1 }),
    (err) => err.code === 'ECONNABORTED',
  )
})

test('isQueuedJob：只认 202 + job_id', () => {
  assert.equal(isQueuedJob({ status: 202, data: { job_id: 'a' } }), true)
  assert.equal(isQueuedJob({ status: 200, data: { analysis: 'x' } }), false)
  assert.equal(isQueuedJob(undefined), false)
})
