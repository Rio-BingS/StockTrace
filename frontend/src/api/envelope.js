/**
 * 列表接口统一外壳的解包（ARC-04 步骤 4c/4d）。
 *
 * 后端列表接口带 ?envelope=1 时返回 {items, pagination, meta}：
 * - items：当前页（或全量）条目；
 * - pagination：null 表示不分页、items 即全量；否则为 {page, page_size, total, total_pages}；
 * - meta：与条目无关的上下文（查询条件、截断标记 truncated 等）。
 *
 * api 层统一请求外壳并在这里解包：res.data 换成 items 数组（组件照旧读 res.data），
 * 完整外壳挂在 res.envelope 上，需要分页 / meta 的地方从那里读。
 * 后端还没升级（返回裸数组或 DRF 分页 results）时原样兼容，res.envelope 为 null。
 * 纯函数，不依赖 axios，便于 node:test 单测。
 */

export const ENVELOPE_PARAM = 'envelope'

/** 在查询参数上加 envelope=1（不改动传入对象） */
export function withEnvelope(params = {}) {
  return { ...params, [ENVELOPE_PARAM]: 1 }
}

/** 是否为 {items: [...], ...} 形状的外壳 */
export function isEnvelope(body) {
  return (
    body != null && typeof body === 'object' && !Array.isArray(body) && Array.isArray(body.items)
  )
}

/** 解包 axios 响应：返回新对象，data 为 items 数组，envelope 为完整外壳（旧形状时为 null） */
export function unwrapEnvelope(res) {
  const body = res?.data
  if (isEnvelope(body)) return { ...res, data: body.items, envelope: body }
  // 旧后端：DRF 分页 {count, next, previous, results}
  if (body != null && typeof body === 'object' && Array.isArray(body.results)) {
    return { ...res, data: body.results, envelope: null }
  }
  return { ...res, envelope: null }
}

/**
 * 逐页取齐分页列表：fetchPage(page) 返回已解包的响应。
 * 按第一页的 pagination.total_pages 继续请求，拼成一个全量结果：
 * data 为全部条目，envelope.pagination 改写为“一页装下全部”。
 * maxPages 防止后端异常时无限请求；超过时只返回已取到的部分并在 meta 标 truncated。
 */
export async function fetchAllPages(fetchPage, { maxPages = 20 } = {}) {
  const first = await fetchPage(1)
  const pagination = first.envelope?.pagination
  if (!pagination || !(pagination.total_pages > 1)) return first
  const items = [...first.data]
  const lastPage = Math.min(pagination.total_pages, maxPages)
  for (let page = 2; page <= lastPage; page++) {
    const res = await fetchPage(page)
    items.push(...res.data)
  }
  const truncated = pagination.total_pages > maxPages
  return {
    ...first,
    data: items,
    envelope: {
      ...first.envelope,
      items,
      pagination: { page: 1, page_size: items.length, total: pagination.total, total_pages: 1 },
      meta: truncated ? { ...first.envelope.meta, truncated: true } : first.envelope.meta,
    },
  }
}
