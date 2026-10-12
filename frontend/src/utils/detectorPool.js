// 探测器「算法推送（沪深300+中证500）」的展示口径：来源筛选、成分池扫描状态、
// 推送列表排序（向下在前，向上未经验证排后）与「加自选」按钮状态。全部是纯函数，
// 数据来自后端 detector/signals 的 source / in_watchlist 字段与 latest_run.pool。

const isNum = (v) => typeof v === 'number' && Number.isFinite(v)

export const POOL_TITLE = '算法推送（沪深300+中证500）'

/** 信号来源筛选，取值与后端 ?source= 一一对应；'' 表示不过滤 */
export const SOURCE_OPTIONS = [
  ['', '全部来源'],
  ['watchlist', '自选'],
  ['pool', '算法推送'],
  ['market', '指数/板块'],
]

const SOURCE_TEXT = { watchlist: '自选', pool: '算法推送', market: '指数/板块' }

/** 来源短标签；旧接口没有 source 字段时返回 '' */
export function sourceText(row) {
  return SOURCE_TEXT[row?.source] || ''
}

/** 列表「对象」列：成分池个股显示「成分池」，自选股显示「自选股」，其余沿用后端类型名 */
export function kindText(row) {
  if (!row) return ''
  if (row.target_kind === 'stock') {
    if (row.source === 'pool') return '成分池'
    if (row.source === 'watchlist') return '自选股'
  }
  return row.target_kind_display || row.target_kind || ''
}

const SEVERITY_RANK = { high: 0, medium: 1, low: 2 }

/** 推送列表排序：向下在前（向上未经验证排后），同方向按严重度、再按强度降序 */
export function sortPoolSignals(items) {
  return [...(items || [])].sort((a, b) => {
    const da = a.direction === 'up' ? 1 : 0
    const db = b.direction === 'up' ? 1 : 0
    if (da !== db) return da - db
    const sa = SEVERITY_RANK[a.severity] ?? 3
    const sb = SEVERITY_RANK[b.severity] ?? 3
    if (sa !== sb) return sa - sb
    return (isNum(b.score) ? b.score : 0) - (isNum(a.score) ? a.score : 0)
  })
}

/** 个股信号能否「一键加自选」：成分池个股且尚未在自选 */
export function canAddToWatchlist(row) {
  return row?.target_kind === 'stock' && row.in_watchlist === false
}

/** 成分池扫描状态一句话；没有成分池信息（旧扫描、未启用）返回 '' */
export function poolStatusText(pool) {
  if (!pool || !pool.status) return ''
  if (pool.status === 'off') {
    return pool.reason ? `成分池未扫描：${pool.reason}` : '成分池未扫描'
  }
  const cov =
    isNum(pool.fresh) && isNum(pool.expected)
      ? `${pool.fresh}/${pool.expected} 只当日日线已入库`
      : ''
  if (pool.status === 'pending') {
    return `成分池日线仍在限速采集${cov ? `（${cov}）` : ''}，齐后自动补跑扫描（最晚 23:45）`
  }
  const parts = []
  if (cov) parts.push(cov)
  if (isNum(pool.skipped_count) && pool.skipped_count > 0)
    parts.push(`${pool.skipped_count} 只序列不可用已跳过`)
  if (isNum(pool.missing_count) && pool.missing_count > 0)
    parts.push(`${pool.missing_count} 只当日缺数未扫`)
  if (
    isNum(pool.signals_raw) &&
    isNum(pool.signals_pushed) &&
    pool.signals_raw > pool.signals_pushed
  ) {
    parts.push(`原始信号 ${pool.signals_raw} 条，按严重度推送前 ${pool.signals_pushed} 条`)
  }
  return parts.length ? `成分池已扫描：${parts.join('，')}` : '成分池已扫描'
}
