/**
 * 市场概览页的图表配置（ARC-09：从 Market.vue 抽出的纯函数，页面只负责取数与渲染）。
 */
import { formatAmount, formatYiAxis } from '../utils/marketFormat.js'
import { DOWN, FLAT, UP, trendColor } from '../constants/colors.js'
import { TOOLTIP_STYLE, AXIS_LABEL_COLOR, AXIS_LINE_COLOR, SPLIT_LINE_COLOR } from './theme.js'

/**
 * 大盘主力净流入序列统一换算为元：东财 fflow / stock_market_fund_flow 主力净流入单位为元；
 * 北向兜底源（stock_hsgt_hist_em 净买额）单位为亿元，换算回元（DAT-04）
 */
export function mainNetInYuan(mainHist) {
  const items = mainHist?.items || []
  const factor = mainHist?.source === 'eastmoney_hsgt_hist_north' ? 1e8 : 1
  return items.map((i) => (i.main_net == null ? null : i.main_net * factor))
}

/** 大盘主力净流入柱状图 */
export function buildFundFlowOption(mainHist) {
  const items = mainHist?.items || []
  if (!items.length) return {}
  const dates = items.map((i) => String(i.date).slice(0, 10))
  const main = mainNetInYuan(mainHist)
  const colors = main.map((v) => trendColor(v))
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      ...TOOLTIP_STYLE,
      valueFormatter: (v) => formatAmount(v, { signed: true }),
    },
    grid: { left: '8%', right: '4%', top: '12%', bottom: '18%' },
    xAxis: {
      type: 'category',
      data: dates,
      axisLabel: { color: AXIS_LABEL_COLOR, fontSize: 11, rotate: 30 },
      axisLine: { lineStyle: { color: AXIS_LINE_COLOR } },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: AXIS_LABEL_COLOR, fontSize: 11, formatter: formatYiAxis },
      splitLine: { lineStyle: { color: SPLIT_LINE_COLOR } },
    },
    series: [
      {
        name: '主力净流入',
        type: 'bar',
        data: main,
        itemStyle: { color: (p) => colors[p.dataIndex] || FLAT },
      },
    ],
  }
}

/** 上涨 / 下跌 / 平盘家数环形图 */
export function buildActivityOption(activity) {
  const a = activity || {}
  const up = a.up ?? 0
  const down = a.down ?? 0
  const flat = a.flat ?? 0
  if (!up && !down && !flat) return {}
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'item', ...TOOLTIP_STYLE },
    series: [
      {
        type: 'pie',
        radius: ['42%', '68%'],
        center: ['50%', '52%'],
        label: { color: '#ccc', fontSize: 12 },
        data: [
          { name: '上涨', value: up, itemStyle: { color: UP } },
          { name: '下跌', value: down, itemStyle: { color: DOWN } },
          { name: '平盘', value: flat, itemStyle: { color: FLAT } },
        ],
      },
    ],
  }
}
