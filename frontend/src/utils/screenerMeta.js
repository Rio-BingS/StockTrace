/**
 * 选股结果的横截面元信息（ALG-12 后端已返回，P3 前端展示）。
 *
 * 后端口径（backend/stocks/screener/engine.py run_screener_with_meta，
 * /api/screener/ 与 /api/screener/ai/ 都把它展开进响应体顶层）：
 * - as_of：'YYYY-MM-DD' | null，本次筛选使用的行情日 = 自选股已收盘日线的最新交易日；
 *   自选股为空或全无已收盘日线时为 null
 * - excluded：[{ stock_id, code, name, last_trade_date: 'YYYY-MM-DD'|null, reason }]，
 *   最新已收盘日线不是 as_of 的股票（停牌/拉取失败/新加入还没日线），不参与本次筛选
 * 旧后端没有这两个字段时按「无元信息」处理，不报错。
 */

/** 从 screener 响应体里取出 { asOf, excluded }，字段缺失或类型不对时给安全默认值 */
export function pickScreenerMeta(data) {
  const asOf = typeof data?.as_of === 'string' && data.as_of ? data.as_of : null
  const excluded = Array.isArray(data?.excluded)
    ? data.excluded.filter((e) => e && typeof e === 'object')
    : []
  return { asOf, excluded }
}

/** 单只被排除股票的一行文案：代码 名称 —— 原因 */
export function excludedLine(item) {
  const head = [item.code, item.name].filter(Boolean).join(' ') || '未知股票'
  const reason =
    item.reason ||
    (item.last_trade_date ? `最新已收盘日线停在 ${item.last_trade_date}` : '无已收盘日线')
  return `${head} —— ${reason}`
}

/** 折叠区标题：N 只自选股未参与本次筛选；没有被排除的股票时返回空串（不展示折叠区） */
export function excludedSummary(excluded, asOf) {
  const n = excluded?.length || 0
  if (!n) return ''
  return asOf
    ? `${n} 只自选股未参与本次筛选（没有 ${asOf} 的已收盘日线）`
    : `${n} 只自选股未参与本次筛选（暂无已收盘日线）`
}
