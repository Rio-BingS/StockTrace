import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import api, { alertApi, dashboardApi, groupApi, presetApi, stockApi } from '../stocks.js'

// 用自定义 adapter 代替网络：记录请求，按路由返回模拟的后端外壳（ARC-04）
let requests = []
let routes = {}

beforeEach(() => {
  requests = []
  routes = {}
  api.defaults.adapter = async (config) => {
    requests.push({ url: config.url, params: { ...config.params }, signal: config.signal })
    const handler = routes[config.url]
    if (!handler) throw new Error(`未模拟的接口 ${config.url}`)
    return {
      data: handler(config.params || {}),
      status: 200,
      statusText: 'OK',
      headers: {},
      config,
    }
  }
})

function envelopeOf(items, meta = {}, pagination = null) {
  return { items, pagination, meta }
}

test('裸数组接口都请求 envelope=1 并解包成数组', async () => {
  const rows = [{ id: 1 }, { id: 2 }]
  routes['/dashboard/'] = () => envelopeOf(rows)
  routes['/stock-groups/'] = () => envelopeOf(rows)
  routes['/screener/presets/'] = () => envelopeOf(rows, { builtin_count: 1 })
  routes['/stocks/search/'] = (p) => envelopeOf(rows, { q: p.q })
  routes['/stocks/7/daily/'] = () => envelopeOf(rows, { code: '600000', date: null, days: 30 })
  routes['/stocks/7/minutes/'] = () => envelopeOf(rows, { code: '600000', date: '2026-09-04' })
  routes['/alerts/events/'] = () => envelopeOf(rows, { limit: 100, truncated: true })

  const calls = [
    [() => dashboardApi.get(), '/dashboard/', {}],
    [() => groupApi.list(), '/stock-groups/', {}],
    [() => presetApi.list(), '/screener/presets/', {}],
    [() => stockApi.search('银行'), '/stocks/search/', { q: '银行' }],
    [() => stockApi.getDaily(7, { days: 30 }), '/stocks/7/daily/', { days: 30 }],
    [
      () => stockApi.getMinutes(7, { date: '2026-09-04' }),
      '/stocks/7/minutes/',
      { date: '2026-09-04' },
    ],
    [() => alertApi.events({ unread: 1 }), '/alerts/events/', { unread: 1 }],
  ]
  for (const [call, url, params] of calls) {
    requests = []
    const res = await call()
    assert.deepEqual(res.data, rows, url)
    assert.ok(res.envelope && Array.isArray(res.envelope.items), url)
    assert.equal(requests.length, 1)
    assert.equal(requests[0].url, url)
    assert.deepEqual(requests[0].params, { ...params, envelope: 1 }, url)
  }
})

test('外壳的 meta 可从 res.envelope 读取', async () => {
  routes['/alerts/events/'] = () =>
    envelopeOf([], { limit: 100, truncated: true, unread_only: false })
  const res = await alertApi.events()
  assert.deepEqual(res.data, [])
  assert.equal(res.envelope.meta.truncated, true)
})

test('signal 照常透传给请求（FEF-06）', async () => {
  routes['/dashboard/'] = () => envelopeOf([])
  const ctrl = new AbortController()
  await dashboardApi.get({ signal: ctrl.signal })
  assert.equal(requests[0].signal, ctrl.signal)
})

test('自选列表按 total_pages 取齐全量，不被分页截断', async () => {
  const all = Array.from({ length: 2500 }, (_, i) => ({ id: i }))
  routes['/stocks/'] = (p) => {
    const size = p.page_size
    const items = all.slice((p.page - 1) * size, p.page * size)
    const pagination = {
      page: p.page,
      page_size: size,
      total: all.length,
      total_pages: Math.ceil(all.length / size),
    }
    return envelopeOf(items, {}, pagination)
  }
  const res = await stockApi.getList()
  assert.equal(res.data.length, 2500)
  assert.deepEqual(
    requests.map((r) => r.params),
    [1, 2, 3].map((page) => ({ page, page_size: 1000, envelope: 1 })),
  )
})

test('自选列表常见情况一次请求', async () => {
  routes['/stocks/'] = (p) =>
    envelopeOf([{ id: 1 }], {}, { page: p.page, page_size: 1000, total: 1, total_pages: 1 })
  const res = await stockApi.getList()
  assert.deepEqual(res.data, [{ id: 1 }])
  assert.equal(requests.length, 1)
})

test('后端尚未升级（裸数组 / DRF 分页）时照样可用', async () => {
  routes['/stock-groups/'] = () => [{ id: 1 }]
  routes['/stocks/'] = () => ({ count: 1, next: null, previous: null, results: [{ id: 9 }] })
  const groups = await groupApi.list()
  assert.deepEqual(groups.data, [{ id: 1 }])
  assert.equal(groups.envelope, null)
  const list = await stockApi.getList()
  assert.deepEqual(list.data, [{ id: 9 }])
})

test('非列表接口不加 envelope 参数', async () => {
  routes['/stocks/7/daily/latest/'] = () => ({ id: 1 })
  routes['/alerts/'] = () => ({ unread_count: 0, items: [] })
  const latest = await stockApi.getDailyLatest(7)
  assert.deepEqual(latest.data, { id: 1 })
  const alerts = await alertApi.list()
  assert.deepEqual(alerts.data, { unread_count: 0, items: [] }, '提醒规则列表不解包')
  assert.deepEqual(
    requests.map((r) => r.params),
    [{}, {}],
  )
})
