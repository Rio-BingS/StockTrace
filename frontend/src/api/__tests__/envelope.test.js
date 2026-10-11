import { test } from 'node:test'
import assert from 'node:assert/strict'
import { fetchAllPages, isEnvelope, unwrapEnvelope, withEnvelope } from '../envelope.js'

test('withEnvelope：加 envelope=1，不改动传入参数（ARC-04）', () => {
  const params = { days: 30 }
  assert.deepEqual(withEnvelope(params), { days: 30, envelope: 1 })
  assert.deepEqual(params, { days: 30 })
  assert.deepEqual(withEnvelope(), { envelope: 1 })
  assert.deepEqual(withEnvelope(undefined), { envelope: 1 })
})

test('isEnvelope：只认 items 为数组的对象', () => {
  assert.equal(isEnvelope({ items: [], pagination: null, meta: {} }), true)
  assert.equal(isEnvelope([]), false)
  assert.equal(isEnvelope(null), false)
  assert.equal(isEnvelope({ items: null }), false)
  assert.equal(isEnvelope({ results: [] }), false)
})

test('unwrapEnvelope：外壳 → data 为 items，完整外壳挂在 envelope', () => {
  const body = { items: [{ id: 1 }], pagination: null, meta: { truncated: false } }
  const res = { status: 200, data: body, headers: {} }
  const out = unwrapEnvelope(res)
  assert.deepEqual(out.data, [{ id: 1 }])
  assert.equal(out.envelope, body)
  assert.equal(out.status, 200)
  assert.equal(res.data, body, '不改动原响应对象')
})

test('unwrapEnvelope：兼容旧后端的裸数组与 DRF 分页', () => {
  const arr = unwrapEnvelope({ data: [{ id: 1 }] })
  assert.deepEqual(arr.data, [{ id: 1 }])
  assert.equal(arr.envelope, null)
  const drf = unwrapEnvelope({
    data: { count: 1, next: null, previous: null, results: [{ id: 2 }] },
  })
  assert.deepEqual(drf.data, [{ id: 2 }])
  assert.equal(drf.envelope, null)
  const other = unwrapEnvelope({ data: { detail: 'x' } })
  assert.deepEqual(other.data, { detail: 'x' })
})

function pagedFetcher(all, pageSize, calls = []) {
  return async (page) => {
    calls.push(page)
    const totalPages = Math.max(1, Math.ceil(all.length / pageSize))
    const items = all.slice((page - 1) * pageSize, page * pageSize)
    return unwrapEnvelope({
      data: {
        items,
        pagination: { page, page_size: pageSize, total: all.length, total_pages: totalPages },
        meta: {},
      },
    })
  }
}

test('fetchAllPages：单页直接返回，不多发请求', async () => {
  const calls = []
  const res = await fetchAllPages(pagedFetcher([1, 2], 10, calls))
  assert.deepEqual(res.data, [1, 2])
  assert.deepEqual(calls, [1])
  assert.equal(res.envelope.pagination.total_pages, 1)
})

test('fetchAllPages：多页逐页取齐，结果不截断', async () => {
  const all = Array.from({ length: 25 }, (_, i) => i)
  const calls = []
  const res = await fetchAllPages(pagedFetcher(all, 10, calls))
  assert.deepEqual(calls, [1, 2, 3])
  assert.deepEqual(res.data, all)
  assert.deepEqual(res.envelope.items, all)
  assert.deepEqual(res.envelope.pagination, { page: 1, page_size: 25, total: 25, total_pages: 1 })
  assert.equal(res.envelope.meta.truncated, undefined)
})

test('fetchAllPages：超过 maxPages 停止并标 truncated', async () => {
  const all = Array.from({ length: 50 }, (_, i) => i)
  const calls = []
  const res = await fetchAllPages(pagedFetcher(all, 10, calls), { maxPages: 2 })
  assert.deepEqual(calls, [1, 2])
  assert.equal(res.data.length, 20)
  assert.equal(res.envelope.meta.truncated, true)
})

test('fetchAllPages：旧后端（无外壳）原样返回', async () => {
  const res = await fetchAllPages(async () => unwrapEnvelope({ data: [{ id: 1 }] }))
  assert.deepEqual(res.data, [{ id: 1 }])
  assert.equal(res.envelope, null)
})
