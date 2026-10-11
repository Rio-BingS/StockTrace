/**
 * 市场页 URL query ⇄ 筛选状态（FEF-15）。
 * KeepAlive 缓存的页面只在首次创建时读一次 query；之后浏览器前进/后退、
 * 点站内带参数的链接时组件不会重建，需要监听 query 并按这里的解析结果重新应用。
 */

function str(v) {
  return Array.isArray(v) ? (v[0] ?? '') : (v ?? '')
}

function pageOf(v) {
  const n = Number(str(v))
  return Number.isInteger(n) && n > 0 ? n : 1
}

export function parseSectorsQuery(query = {}) {
  const raw = str(query.period)
  const period = ['5d', '10d', '20d'].includes(raw) ? raw : 'day'
  return {
    board: str(query.board) === 'concept' ? 'concept' : 'industry',
    period,
    q: str(query.q),
    // 多日轮动只由日度快照供数，只能按净额排序（DAT-15）
    sort: period === 'day' ? str(query.sort) || 'net' : 'net',
    order: str(query.order) === 'asc' ? 'asc' : 'desc',
    page: pageOf(query.page),
  }
}

export function parseEtfRadarQuery(query = {}) {
  return {
    scope: str(query.scope) || 'equity_broad',
    rank: str(query.rank) || 'share',
    sort: str(query.sort),
    order: str(query.order) === 'asc' ? 'asc' : 'desc',
    q: str(query.q),
    minTurnoverYi: str(query.min_turnover_yi),
    page: pageOf(query.page),
    selectedCode: str(query.code),
  }
}

export function parseNationalEtfQuery(query = {}) {
  return { selectedCode: str(query.code) }
}

/** 返回 next 中与当前 refs 取值不同的键；refs 为 { key: Ref } */
export function changedKeys(refs, next) {
  return Object.keys(next).filter(k => String(refs[k].value) !== String(next[k]))
}

/** 把解析结果写回 refs */
export function assignRefs(refs, next) {
  for (const k of Object.keys(next)) refs[k].value = next[k]
}
