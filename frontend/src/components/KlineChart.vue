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
import { formatPct, pctFrom } from '../utils/format.js'

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
  code: { type: String, default: '' },
})

const emit = defineEmits(['date-click'])

// 排序后的数据供点击事件与图表配置共用
const sortedData = computed(() =>
  // 按日期正序排列
  [...props.data].sort((a, b) => new Date(a.trade_date) - new Date(b.trade_date))
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

  // 找最高最低点索引
  let maxIdx = 0, minIdx = 0
  sorted.forEach((d, i) => {
    if (Number(d.high_price) > Number(sorted[maxIdx].high_price)) maxIdx = i
    if (Number(d.low_price) < Number(sorted[minIdx].low_price)) minIdx = i
  })

  return {
    backgroundColor: '#16213e',
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: '#1a1a2e',
      borderColor: '#333',
      textStyle: { color: '#ddd' },
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
        return `
          <div style="font-size:13px">
            <b>${quote.trade_date}</b><br/>
            开盘: ${Number(quote.open_price).toFixed(2)}<br/>
            收盘: ${Number(quote.close_price).toFixed(2)}<br/>
            最高: ${Number(quote.high_price).toFixed(2)}
              ${quote.high_time ? '@ ' + quote.high_time.slice(11, 16) : ''}<br/>
            最低: ${Number(quote.low_price).toFixed(2)}
              ${quote.low_time ? '@ ' + quote.low_time.slice(11, 16) : ''}<br/>
            涨跌幅: ${formatPct(chg)}<br/>
            日内涨幅（较开盘）: ${formatPct(quote.open_close_pct)}<br/>
            振幅: ${Number(quote.high_low_pct).toFixed(2)}%
          </div>
        `
      },
    },
    grid: [
      { left: '10%', right: '8%', top: '12%', height: '55%' },
      { left: '10%', right: '8%', top: '72%', height: '18%' },
    ],
    xAxis: [
      {
        type: 'category',
        data: dates,
        axisLine: { lineStyle: { color: '#444' } },
        axisLabel: { color: '#888', fontSize: 11 },
        gridIndex: 0,
      },
      {
        type: 'category',
        data: dates,
        gridIndex: 1,
        axisLabel: { show: false },
        axisLine: { lineStyle: { color: '#444' } },
        axisTick: { show: false },
      },
    ],
    yAxis: [
      {
        scale: true,
        axisLine: { lineStyle: { color: '#444' } },
        axisLabel: { color: '#888' },
        splitLine: { lineStyle: { color: '#1a2744' } },
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
        fillerColor: 'rgba(233,69,96,0.2)',
        textStyle: { color: '#888' },
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
          color: '#e94560',       // 阳线填充（涨）
          color0: '#00c853',      // 阴线填充（跌）
          borderColor: '#e94560', // 阳线边框
          borderColor0: '#00c853', // 阴线边框
        },
        markPoint: {
          data: [
            {
              type: 'max',
              name: '最高',
              symbol: 'pin',
              symbolSize: 40,
              itemStyle: { color: '#e94560' },
              label: { show: true, fontSize: 10 },
            },
            {
              type: 'min',
              name: '最低',
              symbol: 'pin',
              symbolSize: 40,
              itemStyle: { color: '#00c853' },
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
            // 根据涨跌着色
            const idx = params.dataIndex
            return klineData[idx] && klineData[idx][1] >= klineData[idx][0]
              ? '#e9456080'
              : '#00c85380'
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
      style="height: 500px; width: 100%"
      @click="handleChartClick"
    />
    <div v-else class="no-data">暂无K线数据</div>
  </div>
</template>

<style scoped>
.kline-chart {
  width: 100%;
  min-height: 500px;
}

.no-data {
  text-align: center;
  color: #666;
  padding: 60px;
}
</style>
