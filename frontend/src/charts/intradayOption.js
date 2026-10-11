/**
 * 分时走势图的 ECharts 配置（ARC-09：从 IntradayChart 抽出的纯函数，组件只负责渲染）。
 * bars 为按时间排序的分钟线，prices 为对应收盘价序列，extrema 为阶段高低点，
 * quote 为同一交易日的日线（可为 null），base 为 intradayBase() 的结果。
 */
import { formatVolume } from '../utils/marketFormat.js'
import { escapeHtml } from '../utils/escapeHtml.js'
import { signedPctText as pctTextFrom } from '../utils/intraday.js'
import { ACCENT, DOWN, UP, alpha, trendColor } from '../constants/colors.js'
import { TOOLTIP_STYLE, AXIS_LABEL_COLOR, AXIS_LINE_COLOR, SPLIT_LINE_COLOR } from './theme.js'

export function buildIntradayOption({ bars, prices, extrema, quote, base }) {
  if (!bars.length) return {}
  const sorted = bars
  const signedPctText = price => pctTextFrom(price, base.price)

  const times = sorted.map(d => d.datetime.slice(11, 16))

  const openPrice = quote?.open_price ? Number(quote.open_price) : null
  const closePrice = quote?.close_price ? Number(quote.close_price) : null
  const highPrice = quote?.high_price ? Number(quote.high_price) : null
  const lowPrice = quote?.low_price ? Number(quote.low_price) : null
  const { price: basePrice, isPrevClose } = base
  // 分时线颜色：现价高于基准（昨收）红、低于绿、持平灰；不再固定为红色（FEX-01）
  const lineColor = basePrice ? trendColor(prices[prices.length - 1] - basePrice) : ACCENT

  // 找最高/最低/现价在数据中的索引
  let highIdx = 0, lowIdx = 0
  prices.forEach((p, i) => {
    if (p >= (prices[highIdx] ?? p)) highIdx = i
    if (p <= (prices[lowIdx] ?? p)) lowIdx = i
  })
  const currentIdx = prices.length - 1 // 现价 = 最后一个点

  // 阶段高低点
  const { peaks, valleys } = extrema

  // 构建 MarkPoint 数据
  const markPointData = []

  // 全日最高
  if (highPrice != null) {
    markPointData.push({
      coord: [highIdx, prices[highIdx]],
      name: `最高 ${prices[highIdx].toFixed(2)}`,
      itemStyle: { color: UP },
      symbolOffset: [0, '-60%'],
      label: {
        show: true,
        formatter: () => {
          const pct = signedPctText(prices[highIdx])
          return `高 ${prices[highIdx].toFixed(2)}${pct ? '\n' + pct : ''}`
        },
        fontSize: 10,
        color: UP,
      },
    })
  }

  // 全日最低
  if (lowPrice != null) {
    markPointData.push({
      coord: [lowIdx, prices[lowIdx]],
      name: `最低 ${prices[lowIdx].toFixed(2)}`,
      itemStyle: { color: DOWN },
      symbolOffset: [0, '60%'],
      label: {
        show: true,
        formatter: () => {
          const pct = signedPctText(prices[lowIdx])
          return `低 ${prices[lowIdx].toFixed(2)}${pct ? '\n' + pct : ''}`
        },
        fontSize: 10,
        color: DOWN,
      },
    })
  }

  // 现价（如果不是最高也不是最低时才单独标注）
  if (currentIdx !== highIdx && currentIdx !== lowIdx && prices.length > 0) {
    const curPct = signedPctText(prices[currentIdx])
    markPointData.push({
      coord: [currentIdx, prices[currentIdx]],
      name: `现价 ${prices[currentIdx].toFixed(2)}`,
      itemStyle: { color: '#ffd700' },
      symbolOffset: [0, '-60%'],
      label: {
        show: true,
        formatter: `现 ${prices[currentIdx].toFixed(2)}${curPct ? '\n' + curPct : ''}`,
        fontSize: 10,
        color: '#ffd700',
      },
    })
  }

  // 阶段峰
  peaks.forEach((peak, i) => {
    if (peak.index === highIdx) return // 已标注全日最高
    const pct = signedPctText(peak.price)
    markPointData.push({
      coord: [peak.index, peak.price],
      name: `峰${i + 1}`,
      symbol: 'triangle',
      symbolSize: 10,
      itemStyle: { color: '#ff9800' },
      symbolOffset: [0, '-100%'],
      label: {
        show: true,
        formatter: `${peak.price.toFixed(2)}${pct ? '\n' + pct : ''}`,
        fontSize: 9,
        color: '#ff9800',
      },
    })
  })

  // 阶段谷
  valleys.forEach((valley, i) => {
    if (valley.index === lowIdx) return // 已标注全日最低
    const pct = signedPctText(valley.price)
    markPointData.push({
      coord: [valley.index, valley.price],
      name: `谷${i + 1}`,
      symbol: 'triangle',
      symbolRotate: 180,
      symbolSize: 10,
      itemStyle: { color: '#4fc3f7' },
      symbolOffset: [0, '100%'],
      label: {
        show: true,
        formatter: `${valley.price.toFixed(2)}${pct ? '\n' + pct : ''}`,
        fontSize: 9,
        color: '#4fc3f7',
      },
    })
  })

  // MarkLine：昨收（分时基准）+ 开盘价 + 收盘价水平线
  const markLineData = []
  if (isPrevClose) {
    markLineData.push({
      yAxis: basePrice,
      name: '昨收',
      lineStyle: { color: '#ddd', type: 'solid', width: 1 },
      label: { formatter: `昨收 ${basePrice.toFixed(2)}`, fontSize: 10, color: '#ddd' },
    })
  }
  if (openPrice != null) {
    markLineData.push({
      yAxis: openPrice,
      name: '开盘',
      lineStyle: { color: '#aaa', type: 'dashed', width: 1 },
      label: { formatter: `开盘 ${openPrice.toFixed(2)}`, fontSize: 10, color: '#aaa' },
    })
  }
  if (closePrice != null) {
    markLineData.push({
      yAxis: closePrice,
      name: '收盘',
      lineStyle: { color: '#ffd700', type: 'dashed', width: 1 },
      label: { formatter: `收盘 ${closePrice.toFixed(2)}`, fontSize: 10, color: '#ffd700' },
    })
  }

  return {
    backgroundColor: '#16213e',
    animation: false,
    tooltip: {
      trigger: 'axis',
      ...TOOLTIP_STYLE,
      formatter(params) {
        const p = params[0]
        if (!p) return ''
        const idx = p.dataIndex
        const bar = sorted[idx]
        if (!bar) return ''
        const pct = signedPctText(bar.close)
        return `<div style="font-size:12px">
          <b>${escapeHtml(p.axisValue)}</b><br/>
          价格: ${Number(bar.close).toFixed(2)}${pct ? ` (${base.label} ${pct})` : ''}<br/>
          成交量: ${escapeHtml(formatVolume(bar.volume))}
        </div>`
      },
    },
    // 固定像素边距，窄屏不裁掉价格刻度（FEX-05）
    grid: { left: 56, right: 16, top: 40, bottom: 56 },
    xAxis: {
      type: 'category',
      data: times,
      axisLine: { lineStyle: { color: AXIS_LINE_COLOR } },
      axisLabel: {
        color: AXIS_LABEL_COLOR,
        fontSize: 11,
        interval: Math.max(Math.floor(times.length / 6), 0),
      },
    },
    yAxis: {
      scale: true,
      // 保证昨收线落在可视范围内
      ...(isPrevClose ? {
        min: v => Math.min(v.min, basePrice),
        max: v => Math.max(v.max, basePrice),
      } : {}),
      axisLine: { lineStyle: { color: AXIS_LINE_COLOR } },
      axisLabel: { color: AXIS_LABEL_COLOR, fontSize: 11 },
      splitLine: { lineStyle: { color: SPLIT_LINE_COLOR } },
    },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      {
        type: 'slider',
        bottom: '3%',
        height: 12,
        borderColor: '#333',
        fillerColor: alpha(ACCENT, 0.2),
        textStyle: { color: AXIS_LABEL_COLOR },
      },
    ],
    series: [{
      name: '价格',
      type: 'line',
      data: prices,
      smooth: false,
      symbol: 'none',
      lineStyle: { color: lineColor, width: 1.5 },
      areaStyle: { color: alpha(lineColor, 0.08) },
      markPoint: {
        data: markPointData,
        animation: false,
      },
      markLine: {
        data: markLineData,
        symbol: 'none',
        animation: false,
      },
    }],
  }
}

/** 阶段高低点表：峰谷按时间排序，价格两位小数，百分比相对 base */
export function buildExtremaList({ bars, extrema, base }) {
  if (!bars.length) return []
  const row = (type, e) => ({
    type,
    time: bars[e.index].datetime.slice(11, 16),
    price: e.price.toFixed(2),
    pct: pctTextFrom(e.price, base.price),
  })
  return [
    ...extrema.peaks.map(e => row('峰', e)),
    ...extrema.valleys.map(e => row('谷', e)),
  ].sort((a, b) => a.time.localeCompare(b.time))
}
