<script setup>
import { computed } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import {
  TitleComponent,
  TooltipComponent,
  GridComponent,
  DataZoomComponent,
  MarkPointComponent,
  MarkLineComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { formatPct, formatNum, pctClass, pctFrom } from '../utils/format.js'
import { escapeHtml } from '../utils/escapeHtml.js'

use([
  LineChart,
  TitleComponent,
  TooltipComponent,
  GridComponent,
  DataZoomComponent,
  MarkPointComponent,
  MarkLineComponent,
  CanvasRenderer,
])

const props = defineProps({
  minuteData: { type: Array, default: () => [] },
  dailyQuote: { type: Object, default: () => ({}) },
  date: { type: String, default: '' },
})

// 百分比基准：A 股惯例为昨收；拿不到昨收（如首个交易日）时才回退开盘价，并在界面注明基准
const baseInfo = computed(() => {
  const q = props.dailyQuote || {}
  if (q.prev_close != null && Number(q.prev_close)) {
    return { price: Number(q.prev_close), label: '较昨收', isPrevClose: true }
  }
  if (q.open_price != null && Number(q.open_price)) {
    return { price: Number(q.open_price), label: '较开盘', isPrevClose: false }
  }
  return { price: null, label: '', isPrevClose: false }
})

// 相对基准的带符号百分比文本，基准缺失时返回 ''
function signedPctText(price) {
  const v = pctFrom(price, baseInfo.value.price)
  return v == null ? '' : formatPct(v)
}

// 局部极值检测
function findLocalExtrema(prices, windowSize = 20) {
  if (prices.length < windowSize * 2 + 1) return { peaks: [], valleys: [] }

  const peaks = []
  const valleys = []
  const fullRange = Math.max(...prices) - Math.min(...prices)
  const minSwing = fullRange * 0.01 // 至少为全天振幅的1%

  for (let i = windowSize; i < prices.length - windowSize; i++) {
    const leftSlice = prices.slice(i - windowSize, i)
    const rightSlice = prices.slice(i + 1, i + windowSize + 1)
    const current = prices[i]

    const isPeak = leftSlice.every(v => v <= current) && rightSlice.every(v => v <= current)
    const isValley = leftSlice.every(v => v >= current) && rightSlice.every(v => v >= current)

    if (isPeak) {
      // 与上一个峰/谷比较，过滤过小幅波动
      const lastPeak = peaks.length > 0 ? peaks[peaks.length - 1] : null
      if (!lastPeak || Math.abs(current - lastPeak.price) >= minSwing) {
        peaks.push({ index: i, price: current })
      }
    }
    if (isValley) {
      const lastValley = valleys.length > 0 ? valleys[valleys.length - 1] : null
      if (!lastValley || Math.abs(current - lastValley.price) >= minSwing) {
        valleys.push({ index: i, price: current })
      }
    }
  }

  return { peaks, valleys }
}

// 走势图配置
const chartOption = computed(() => {
  if (!props.minuteData.length) return {}

  const sorted = [...props.minuteData].sort(
    (a, b) => new Date(a.datetime) - new Date(b.datetime)
  )

  const times = sorted.map(d => d.datetime.slice(11, 16))
  const prices = sorted.map(d => Number(d.close))

  const openPrice = props.dailyQuote?.open_price ? Number(props.dailyQuote.open_price) : null
  const closePrice = props.dailyQuote?.close_price ? Number(props.dailyQuote.close_price) : null
  const highPrice = props.dailyQuote?.high_price ? Number(props.dailyQuote.high_price) : null
  const lowPrice = props.dailyQuote?.low_price ? Number(props.dailyQuote.low_price) : null
  const { price: basePrice, isPrevClose } = baseInfo.value

  // 找最高/最低/现价在数据中的索引
  let highIdx = 0, lowIdx = 0
  prices.forEach((p, i) => {
    if (p >= (prices[highIdx] ?? p)) highIdx = i
    if (p <= (prices[lowIdx] ?? p)) lowIdx = i
  })
  const currentIdx = prices.length - 1 // 现价 = 最后一个点

  // 阶段高低点检测
  const { peaks, valleys } = findLocalExtrema(prices)

  // 构建 MarkPoint 数据
  const markPointData = []

  // 全日最高
  if (highPrice != null) {
    markPointData.push({
      coord: [highIdx, prices[highIdx]],
      name: `最高 ${prices[highIdx].toFixed(2)}`,
      itemStyle: { color: '#e94560' },
      symbolOffset: [0, '-60%'],
      label: {
        show: true,
        formatter: () => {
          const pct = signedPctText(prices[highIdx])
          return `高 ${prices[highIdx].toFixed(2)}${pct ? '\n' + pct : ''}`
        },
        fontSize: 10,
        color: '#e94560',
      },
    })
  }

  // 全日最低
  if (lowPrice != null) {
    markPointData.push({
      coord: [lowIdx, prices[lowIdx]],
      name: `最低 ${prices[lowIdx].toFixed(2)}`,
      itemStyle: { color: '#00c853' },
      symbolOffset: [0, '60%'],
      label: {
        show: true,
        formatter: () => {
          const pct = signedPctText(prices[lowIdx])
          return `低 ${prices[lowIdx].toFixed(2)}${pct ? '\n' + pct : ''}`
        },
        fontSize: 10,
        color: '#00c853',
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
      backgroundColor: '#1a1a2e',
      borderColor: '#333',
      textStyle: { color: '#ddd', fontSize: 12 },
      formatter(params) {
        const p = params[0]
        if (!p) return ''
        const idx = p.dataIndex
        const bar = sorted[idx]
        if (!bar) return ''
        const pct = signedPctText(bar.close)
        return `<div style="font-size:12px">
          <b>${escapeHtml(p.axisValue)}</b><br/>
          价格: ${Number(bar.close).toFixed(2)}${pct ? ` (${baseInfo.value.label} ${pct})` : ''}<br/>
          成交量: ${escapeHtml(bar.volume || '-')}
        </div>`
      },
    },
    grid: { left: '8%', right: '5%', top: '12%', bottom: '22%' },
    xAxis: {
      type: 'category',
      data: times,
      axisLine: { lineStyle: { color: '#444' } },
      axisLabel: {
        color: '#888',
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
      axisLine: { lineStyle: { color: '#444' } },
      axisLabel: { color: '#888', fontSize: 11 },
      splitLine: { lineStyle: { color: '#1a2744' } },
    },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      {
        type: 'slider',
        bottom: '3%',
        height: 12,
        borderColor: '#333',
        fillerColor: 'rgba(233,69,96,0.2)',
        textStyle: { color: '#888' },
      },
    ],
    series: [{
      name: '价格',
      type: 'line',
      data: prices,
      smooth: false,
      symbol: 'none',
      lineStyle: { color: '#e94560', width: 1.5 },
      areaStyle: { color: 'rgba(233,69,96,0.08)' },
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
})

// 表格数据
const tableData = computed(() => {
  const q = props.dailyQuote || {}
  return {
    open_price: q.open_price,
    close_price: q.close_price,
    high_price: q.high_price,
    low_price: q.low_price,
    current_price: props.minuteData.length
      ? props.minuteData[props.minuteData.length - 1].close
      : null,
    prev_close: q.prev_close,
    change_pct: q.change_pct,
    open_close_pct: q.open_close_pct,
    high_low_pct: q.high_low_pct,
  }
})

// 阶段高低点列表
const extremaList = computed(() => {
  if (!props.minuteData.length) return []
  const sorted = [...props.minuteData].sort(
    (a, b) => new Date(a.datetime) - new Date(b.datetime)
  )
  const prices = sorted.map(d => Number(d.close))
  const { peaks, valleys } = findLocalExtrema(prices)

  const list = []
  peaks.forEach(p => {
    const pct = signedPctText(p.price)
    list.push({
      type: '峰',
      time: sorted[p.index].datetime.slice(11, 16),
      price: p.price.toFixed(2),
      pct,
    })
  })
  valleys.forEach(v => {
    const pct = signedPctText(v.price)
    list.push({
      type: '谷',
      time: sorted[v.index].datetime.slice(11, 16),
      price: v.price.toFixed(2),
      pct,
    })
  })
  // 按时间排序
  list.sort((a, b) => a.time.localeCompare(b.time))
  return list
})
</script>

<template>
  <div class="intraday-chart">
    <div class="chart-header">
      <h3>{{ date }} 当日走势</h3>
      <span v-if="baseInfo.label" class="base-note">百分比{{ baseInfo.label }}</span>
    </div>

    <v-chart
      v-if="minuteData.length"
      :option="chartOption"
      :autoresize="true"
      style="height: 400px; width: 100%"
    />
    <div v-else class="no-data">暂无分钟走势数据</div>

    <!-- 数据表格 -->
    <div v-if="minuteData.length" class="data-table-section">
      <table class="quote-table">
        <tbody>
          <tr>
            <td class="tlabel">开盘价</td>
            <td>{{ formatNum(tableData.open_price) }}</td>
            <td class="tlabel">收盘价</td>
            <td>{{ formatNum(tableData.close_price) }}</td>
          </tr>
          <tr>
            <td class="tlabel">最高价</td>
            <td class="up">{{ formatNum(tableData.high_price) }}</td>
            <td class="tlabel">最低价</td>
            <td class="down">{{ formatNum(tableData.low_price) }}</td>
          </tr>
          <tr>
            <td class="tlabel">现价</td>
            <td :class="pctClass(tableData.change_pct)">{{ formatNum(tableData.current_price) }}</td>
            <td class="tlabel">涨跌幅</td>
            <td :class="pctClass(tableData.change_pct)">{{ formatPct(tableData.change_pct) }}</td>
          </tr>
          <tr>
            <td class="tlabel">昨收</td>
            <td>{{ formatNum(tableData.prev_close) }}</td>
            <td class="tlabel">日内涨幅（较开盘）</td>
            <td :class="pctClass(tableData.open_close_pct)">{{ formatPct(tableData.open_close_pct) }}</td>
          </tr>
          <tr>
            <td class="tlabel">振幅</td>
            <td>{{ formatPct(tableData.high_low_pct) }}</td>
            <td class="tlabel"></td>
            <td></td>
          </tr>
        </tbody>
      </table>

      <!-- 阶段高低点 -->
      <div v-if="extremaList.length" class="extrema-section">
        <h4>阶段高低点</h4>
        <table class="extrema-table">
          <thead>
            <tr>
              <th>类型</th>
              <th>时间</th>
              <th>价格</th>
              <th>{{ baseInfo.label ? '涨跌（' + baseInfo.label + '）' : '涨跌' }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(item, i) in extremaList" :key="i">
              <td :class="item.type === '峰' ? 'up' : 'down'">{{ item.type }}</td>
              <td>{{ item.time }}</td>
              <td>{{ item.price }}</td>
              <td :class="pctClass(item.pct)">{{ item.pct }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<style scoped>
.intraday-chart {
  background: #16213e;
  border-radius: 8px;
  padding: 16px;
}

.chart-header {
  margin-bottom: 12px;
}

.chart-header h3 {
  color: #eee;
  font-size: 16px;
}

.base-note {
  color: #888;
  font-size: 12px;
}

.no-data {
  text-align: center;
  color: #666;
  padding: 60px;
}

.data-table-section {
  margin-top: 16px;
}

.quote-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
}

.quote-table td {
  padding: 8px 12px;
  border-bottom: 1px solid #1a2744;
}

.tlabel {
  color: #888;
  font-size: 13px;
}

.quote-table td:not(.tlabel) {
  color: #ddd;
  font-weight: bold;
}

.up { color: #e94560; font-weight: bold; }
.down { color: #00c853; font-weight: bold; }

.extrema-section {
  margin-top: 16px;
}

.extrema-section h4 {
  color: #ccc;
  font-size: 14px;
  margin-bottom: 8px;
}

.extrema-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.extrema-table th {
  background: #0f3460;
  color: #aaa;
  padding: 8px 12px;
  text-align: left;
  font-weight: normal;
}

.extrema-table td {
  padding: 6px 12px;
  color: #ddd;
  border-bottom: 1px solid #1a2744;
}
</style>
