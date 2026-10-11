/** 分时图百分比基准与标注文本（FEF-13） */
import { toNum } from './number.js'
import { formatPct, pctFrom } from './format.js'

/**
 * 百分比基准：A 股惯例为昨收；拿不到昨收（如首个交易日）时才回退开盘价，
 * 并返回 label 让界面注明基准。都没有时 price 为 null，标注不显示百分比。
 */
export function intradayBase(quote) {
  const q = quote || {}
  const prev = toNum(q.prev_close)
  if (prev) return { price: prev, label: '较昨收', isPrevClose: true }
  const open = toNum(q.open_price)
  if (open) return { price: open, label: '较开盘', isPrevClose: false }
  return { price: null, label: '', isPrevClose: false }
}

/** 相对基准的带符号百分比文本（统一走 formatPct，不会出现 "+-1.23%"）；基准缺失时返回 '' */
export function signedPctText(price, basePrice) {
  const v = pctFrom(price, basePrice)
  return v == null ? '' : formatPct(v)
}

/**
 * 分时图对应的日线：只用同一交易日的行，找不到时返回 null（宁可显示"较开盘/无百分比"，
 * 也不拿别的交易日的昨收当基准）
 */
export function quoteForDate(dailyRows, latestQuote, date) {
  const hit = (dailyRows || []).find((d) => d.trade_date === date)
  if (hit) return hit
  return latestQuote && latestQuote.trade_date === date ? latestQuote : null
}
