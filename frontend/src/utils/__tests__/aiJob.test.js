// 运行：npm test（Node 内置 node:test，无需额外依赖）
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { aiJobPhaseText, isQueuedJob, pollAiJob, runAiJob } from '../aiJob.js'

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

test('runAiJob：202 时轮询任务并回调排队状态', async () => {
  const seen = []
  const asked = []
  const next = seq({ status: 'running' }, { status: 'done', result: { comment: 'ok' } })
  const data = await runAiJob(
    async () => ({ status: 202, data: { job_id: 'j1', status: 'queued' } }),
    (id) => {
      asked.push(id)
      return next()
    },
    { intervalMs: 1, onStatus: (j) => seen.push(j.status) },
  )
  assert.deepEqual(data, { comment: 'ok' })
  assert.equal(asked[0], 'j1')
  assert.deepEqual(seen, ['queued', 'running'])
})

test('runAiJob：旧后端直接 200 时按同步结果返回', async () => {
  let polled = false
  const data = await runAiJob(
    async () => ({ status: 200, data: { count: 1, results: [] } }),
    async () => {
      polled = true
    },
  )
  assert.deepEqual(data, { count: 1, results: [] })
  assert.equal(polled, false)
})

test('runAiJob：任务失败抛出与 axios 同形的错误', async () => {
  await assert.rejects(
    runAiJob(
      async () => ({ status: 202, data: { job_id: 'j2' } }),
      async () => ({
        data: { status: 'failed', http_status: 400, result: { detail: '条件无法执行' } },
      }),
      { intervalMs: 1 },
    ),
    (err) => err.response.status === 400 && err.response.data.detail === '条件无法执行',
  )
})

test('aiJobPhaseText：排队显示前面任务数，执行中用调用方文案', () => {
  assert.equal(
    aiJobPhaseText({ status: 'queued', queued_ahead: 2 }, 'AI 翻译中'),
    '已排队（前面 2 个）',
  )
  assert.equal(aiJobPhaseText({ status: 'queued' }, 'AI 翻译中'), '已排队')
  assert.equal(aiJobPhaseText({ status: 'running' }, 'AI 翻译中'), 'AI 翻译中')
  assert.equal(aiJobPhaseText(null, 'AI 点评中'), 'AI 点评中')
})
