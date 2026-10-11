/**
 * 选股手动条件的校验与序列化（FEF-09），纯函数，便于 node:test 单测。
 * 以前不完整的条件会被静默丢弃，用户以为多个条件共同生效，实际只执行了一部分。
 * 现在逐行给出错误，有任何一行不完整就不允许执行。
 */

// 条件行的稳定 key：增删行时 v-for 不能用下标，否则删除中间一行后
// 下面各行的输入框状态（焦点、未提交的输入法文本）会错位到别的行（FEF-17）
let condUid = 0

/** 新建一行条件（带稳定 uid，不随序列化提交给后端） */
export function newCondition(fields = { field: 'change_pct', op: 'gt', value: '' }) {
  condUid += 1
  return { uid: condUid, ...fields }
}

function isBlank(v) {
  return v === undefined || v === null || String(v).trim() === ''
}

function toNumber(v) {
  return isBlank(v) ? NaN : Number(v)
}

/**
 * 单个条件的错误文案；完整有效时返回 ''。
 * @param {{field:string, op:string, value:any, value2?:any}} cond
 * @param {Set<string>} boolFields 布尔字段集合（值只能是 1/0）
 */
export function conditionError(cond, boolFields = new Set()) {
  if (!cond || !cond.field) return '请选择指标'
  if (boolFields.has(cond.field)) {
    return ['1', '0'].includes(String(cond.value)) ? '' : '请选择是/否'
  }
  if (isBlank(cond.value)) return cond.op === 'between' ? '请填写下限' : '请填写数值'
  const lo = toNumber(cond.value)
  if (!Number.isFinite(lo)) return '数值无效'
  if (cond.op === 'between') {
    if (isBlank(cond.value2)) return '请填写上限'
    const hi = toNumber(cond.value2)
    if (!Number.isFinite(hi)) return '上限数值无效'
    if (lo > hi) return '下限不能大于上限'
  }
  return ''
}

/** 每一行的错误文案数组（与 conds 下标对应，'' 表示该行有效） */
export function conditionErrors(conds, boolFields) {
  return (conds || []).map((c) => conditionError(c, boolFields))
}

/**
 * 序列化为后端 spec 的 conditions；任一行无效返回 null（不再静默丢弃）。
 */
export function buildConditions(conds, boolFields) {
  if (!conds || !conds.length) return null
  if (conditionErrors(conds, boolFields).some(Boolean)) return null
  return conds.map((c) => {
    const op = boolFields?.has(c.field) ? 'eq' : c.op
    return {
      field: c.field,
      op,
      value: op === 'between' ? [Number(c.value), Number(c.value2)] : Number(c.value),
    }
  })
}

/** 结果行能否跳转详情：只有已在库中（有 stock_id）的股票才有详情页 */
export function hasDetail(row) {
  return !!row && row.stock_id !== null && row.stock_id !== undefined && row.stock_id !== ''
}
