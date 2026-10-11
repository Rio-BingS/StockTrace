/**
 * 复权口径的中文标签（DAT-14 后端已返回，P3 前端标注）。
 *
 * 后端口径（backend/stocks/market/etf.py _ETF_HIST_ADJUST）：ETF 详情的
 * history.adjust 取值 'qfq'（东财源，前复权）| 'none'（新浪备源，不复权）；
 * 'hfq' 目前不会出现，预留以免日后改口径时页面显示英文代码。
 * 缺失或未知取值返回空串，由页面决定不展示，不猜测口径。
 */
export const ADJUST_LABELS = { qfq: '前复权', hfq: '后复权', none: '不复权' }

const ADJUST_HINTS = {
  qfq: '以最新价为基准回溯调整历史价格，与当日实际成交价可能不同；区间涨跌与 5/20/60 日涨跌按此口径计算',
  hfq: '以上市首日为基准向后调整价格，数值与实际成交价不同',
  none: '未复权，除权除息日价格会出现跳空，区间涨跌可能失真',
}

// 只认自有键：'toString' 之类原型属性不能被当成口径
const own = (map, key) => (typeof key === 'string' && Object.hasOwn(map, key) ? map[key] : '')

export function adjustLabel(adjust) {
  return own(ADJUST_LABELS, adjust)
}

export function adjustHint(adjust) {
  return own(ADJUST_HINTS, adjust)
}

/** 图表价格序列名：带口径，图例与 tooltip 同步显示；口径未知时保持原名 */
export function priceSeriesName(adjust, base = '收盘价') {
  const label = adjustLabel(adjust)
  return label ? `${base}（${label}）` : base
}
