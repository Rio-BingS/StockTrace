<script setup>
import { computed } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CandlestickChart, BarChart } from 'echarts/charts'
import {
  TitleComponent,
  TooltipComponent,
  GridComponent,
  DataZoomComponent,
  MarkPointComponent,
  MarkLineComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { formatNum, formatPct, formatRate, pctFrom } from '../utils/format.js'
import { formatAmount, formatVolume } from '../utils/marketFormat.js'
import { escapeHtml } from '../utils/escapeHtml.js'
import { byKey } from '../utils/sortBy.js'
import { ACCENT, DOWN, FLAT, UP, alpha, trendColor } from '../constants/colors.js'
// 图表 tooltip / 坐标轴配色统一取自 charts/theme.js（ARC-06）
import { TOOLTIP_STYLE, AXIS_LABEL_COLOR, AXIS_LINE_COLOR, SPLIT_LINE_COLOR } from '../charts/theme.js'

use([
  CandlestickChart,
  BarChart,
  TitleComponent,
  TooltipComponent,
  GridComponent,
  DataZoomComponent,
  MarkPointComponent,
  MarkLineComponent,
  CanvasRenderer,
])

const props = defineProps({
  data: { type: Array, default: () => [] },
})

const emit = defineEmits(['date-click'])

// 排序后的数据供点击事件与图表配置共用
const sortedData = computed(() =>
  // 按日期正序排列（字符串比较，避免 Safari 日期解析问题）
  [...props.data].sort(byKey('trade_date'))
)

// 将日K数据转为 ECharts 格式
const chartOption = computed(() => {
  const sorted = sortedData.value
  if (!sorted.length) return {}

  const dates = sorted.map(d => d.trade_date)
  // ECharts K线数据: [open, close, low, high]
  const klineData = sorted.map(d => [
    Number(d.open_price),
    Number(d.close_price),
    Number(d.low_price),
    Number(d.high_price),
  ])
  const volumes = sorted.map(d => d.volume || 0)

  return {
    backgroundColor: '#16213e',
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      ...TOOLTIP_STYLE,
      formatter(params) {
        const d = params[0]
        if (!d) return ''
        const idx = d.dataIndex
        const quote = sorted[idx]
        if (!quote) return ''
        // 涨跌幅相对昨收：优先后端 change_pct；缺失时用 prev_close 或前一根K线收盘计算
        let chg = quote.change_pct
        if (chg == null) {
          const base = quote.prev_close ?? (idx > 0 ? sorted[idx - 1].close_price : null)
          chg = pctFrom(quote.close_price, base)
        }
        // tooltip 返回 HTML，接口字段一律转义后再拼（SEC-09）
        return `
          <div style="font-size:13px">
            <b>${escapeHtml(quote.trade_date)}</b><br/>
            开盘: ${formatNum(quote.open_price)}<br/>
            收盘: ${formatNum(quote.close_price)}<br/>
            最高: ${formatNum(quote.high_price)}
              ${quote.high_time ? '@ ' + escapeHtml(quote.high_time.slice(11, 16)) : ''}<br/>
            最低: ${formatNum(quote.low_price)}
              ${quote.low_time ? '@ ' + escapeHtml(quote.low_time.slice(11, 16)) : ''}<br/>
            涨跌幅: ${formatPct(chg)}<br/>
            日内涨幅（较开盘）: ${formatPct(quote.open_close_pct)}<br/>
            振幅: ${formatRate(quote.high_low_pct)}<br/>
            成交量: ${formatVolume(quote.volume)}<br/>
            成交额: ${formatAmount(quote.turnover)}
          </div>
        `
      },
    },
    // 固定像素边距：百分比边距在窄屏上会把价格刻度挤出画布；两个 grid 左右一致保证 K 线与量柱对齐（FEX-05）
    grid: [
      { left: 56, right: 16, top: 36, height: '55%' },
      { left: 56, right: 16, top: '72%', height: '18%' },
    ],
    xAxis: [
      {
        type: 'category',
        data: dates,
        axisLine: { lineStyle: { color: AXIS_LINE_COLOR } },
        axisLabel: { color: AXIS_LABEL_COLOR, fontSize: 11 },
        gridIndex: 0,
      },
      {
        type: 'category',
        data: dates,
        gridIndex: 1,
        axisLabel: { show: false },
        axisLine: { lineStyle: { color: AXIS_LINE_COLOR } },
        axisTick: { show: false },
      },
    ],
    yAxis: [
      {
        scale: true,
        axisLine: { lineStyle: { color: AXIS_LINE_COLOR } },
        axisLabel: { color: AXIS_LABEL_COLOR },
        splitLine: { lineStyle: { color: SPLIT_LINE_COLOR } },
        gridIndex: 0,
      },
      {
        scale: true,
        gridIndex: 1,
        axisLabel: { show: false },
        splitLine: { show: false },
      },
    ],
    dataZoom: [
      {
        type: 'inside',
        xAxisIndex: [0, 1],
        start: Math.max(0, (1 - 30 / dates.length) * 100),
        end: 100,
      },
      {
        type: 'slider',
        xAxisIndex: [0, 1],
        bottom: '2%',
        height: 12,
        borderColor: '#333',
        fillerColor: alpha(ACCENT, 0.2),
        textStyle: { color: AXIS_LABEL_COLOR },
      },
    ],
    series: [
      {
        name: 'K线',
        type: 'candlestick',
        data: klineData,
        xAxisIndex: 0,
        yAxisIndex: 0,
        // A股配色：红涨绿跌
        itemStyle: {
          color: UP, // 阳线填充（涨）
          color0: DOWN, // 阴线填充（跌）
          borderColor: UP, // 阳线边框
          borderColor0: DOWN, // 阴线边框
        },
        // 蜡烛图默认按收盘价维度取极值，需显式指定最高/最低价维度（FEF-12）
        markPoint: {
          data: [
            {
              type: 'max',
              valueDim: 'highest',
              name: '最高',
              symbol: 'pin',
              symbolSize: 40,
              itemStyle: { color: UP },
              label: { show: true, fontSize: 10 },
            },
            {
              type: 'min',
              valueDim: 'lowest',
              name: '最低',
              symbol: 'pin',
              symbolSize: 40,
              itemStyle: { color: DOWN },
              label: { show: true, fontSize: 10 },
            },
          ],
        },
      },
      {
        name: '成交量',
        type: 'bar',
        data: volumes,
        xAxisIndex: 1,
        yAxisIndex: 1,
        itemStyle: {
          color: function (params) {
            // 根据当日阴阳着色，平盘（收=开）为灰色（FEX-01）
            const k = klineData[params.dataIndex]
            return alpha(k ? trendColor(k[1] - k[0]) : FLAT, 0.5)
          },
        },
      },
    ],
  }
})

function handleChartClick(params) {
  if (params.seriesType === 'candlestick' && sortedData.value[params.dataIndex]) {
    const date = sortedData.value[params.dataIndex].trade_date
    emit('date-click', date)
  }
}
</script>

<template>
  <div class="kline-chart">
    <v-chart
      v-if="data.length"
      :option="chartOption"
      :autoresize="true"
      class="kline-canvas"
      @click="handleChartClick"
    />
    <div v-else class="no-data">暂无K线数据</div>
  </div>
</template>

<style scoped>
.kline-chart {
  width: 100%;
}

.kline-canvas {
  width: 100%;
  height: 500px;
}

@media (max-width: 600px) {
  .kline-canvas {
    height: 380px;
  }
}

.no-data {
  text-align: center;
  color: #919191;
  padding: 60px;
}
</style>
