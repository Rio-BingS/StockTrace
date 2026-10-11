/**
 * 看板持仓（成本价/持股数）相关纯函数（FEF-08），便于 node:test 单测。
 */

/** 盈亏：仅填了成本价、持股数且有现价的行参与；否则返回 null */
export function computePnl(item) {
  if (!item || item.cost_price == null || item.quantity == null || item.close_price == null) return null
  const pnl = (Number(item.close_price) - Number(item.cost_price)) * Number(item.quantity)
  return Number.isFinite(pnl) ? pnl : null
}

function serverDraft(item) {
  return {
    cost_price: item.cost_price ?? '',
    quantity: item.quantity ?? '',
    dirty: false,
  }
}

/**
 * 用服务端最新数据同步草稿：用户正在编辑（dirty）的行保留草稿，
 * 其余行跟随服务端；已不在列表中的行丢弃。返回新对象，不修改入参。
 */
export function syncDrafts(prev, items) {
  const next = {}
  for (const item of items || []) {
    const old = prev?.[item.code]
    next[item.code] = old?.dirty ? old : serverDraft(item)
  }
  return next
}

/**
 * 校验持仓草稿。空串表示清空（null）。
 * 成本价须为大于 0 的有限数；持股数须为不小于 0 的整数。
 * 返回 { ok: true, cost, qty } 或 { ok: false, error }。
 */
export function validateHolding(draft) {
  const rawCost = draft?.cost_price ?? ''
  const rawQty = draft?.quantity ?? ''
  const cost = String(rawCost).trim() === '' ? null : Number(rawCost)
  const qty = String(rawQty).trim() === '' ? null : Number(rawQty)
  if (cost !== null && !(Number.isFinite(cost) && cost > 0)) {
    return { ok: false, error: '成本价需为大于 0 的数字' }
  }
  if (qty !== null && !(Number.isInteger(qty) && qty >= 0)) {
    return { ok: false, error: '持股数需为不小于 0 的整数' }
  }
  return { ok: true, cost, qty }
}
