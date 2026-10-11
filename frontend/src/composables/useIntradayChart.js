import { computed } from 'vue'
import { byKey } from '../utils/sortBy.js'
import { findLocalExtrema } from '../utils/extrema.js'
import { intradayBase } from '../utils/intraday.js'
import { buildExtremaList, buildIntradayOption } from '../charts/intradayOption.js'

/**
 * 分时图的数据与图表配置（ARC-09）：排序、极值、基准只算一次（FEX-07），
 * 组件只负责渲染。props 需含 minuteData / dailyQuote。
 */
export function useIntradayChart(props) {
  // 百分比基准：昨收优先，缺失时回退开盘并注明（FEF-13）
  const baseInfo = computed(() => intradayBase(props.dailyQuote))
  const sortedBars = computed(() => [...props.minuteData].sort(byKey('datetime')))
  const priceSeries = computed(() => sortedBars.value.map((d) => Number(d.close)))
  const extrema = computed(() => findLocalExtrema(priceSeries.value))

  const chartOption = computed(() =>
    buildIntradayOption({
      bars: sortedBars.value,
      prices: priceSeries.value,
      extrema: extrema.value,
      quote: props.dailyQuote,
      base: baseInfo.value,
    }),
  )

  const tableData = computed(() => {
    const q = props.dailyQuote || {}
    const prices = priceSeries.value
    return {
      open_price: q.open_price,
      close_price: q.close_price,
      high_price: q.high_price,
      low_price: q.low_price,
      // 现价取按时间排序后的最后一根，不依赖接口返回顺序
      current_price: prices.length ? prices[prices.length - 1] : null,
      prev_close: q.prev_close,
      change_pct: q.change_pct,
      open_close_pct: q.open_close_pct,
      high_low_pct: q.high_low_pct,
    }
  })

  const extremaList = computed(() =>
    buildExtremaList({
      bars: sortedBars.value,
      extrema: extrema.value,
      base: baseInfo.value,
    }),
  )

  return { baseInfo, chartOption, tableData, extremaList }
}
