<script setup>
// 分时走势图：只负责渲染；数据整理与图表配置见 useIntradayChart / charts/intradayOption（ARC-09）
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
import { formatPct, formatNum, formatRate, pctClass } from '../utils/format.js'
import { useIntradayChart } from '../composables/useIntradayChart.js'

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

const { baseInfo, chartOption, tableData, extremaList } = useIntradayChart(props)
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
      class="intraday-canvas"
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
            <td :class="pctClass(tableData.change_pct)">
              {{ formatNum(tableData.current_price) }}
            </td>
            <td class="tlabel">涨跌幅</td>
            <td :class="pctClass(tableData.change_pct)">{{ formatPct(tableData.change_pct) }}</td>
          </tr>
          <tr>
            <td class="tlabel">昨收</td>
            <td>{{ formatNum(tableData.prev_close) }}</td>
            <td class="tlabel">日内涨幅（较开盘）</td>
            <td :class="pctClass(tableData.open_close_pct)">
              {{ formatPct(tableData.open_close_pct) }}
            </td>
          </tr>
          <tr>
            <td class="tlabel">振幅</td>
            <td>{{ formatRate(tableData.high_low_pct) }}</td>
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
  color: #909090;
  font-size: 12px;
}

.intraday-canvas {
  width: 100%;
  height: 400px;
}

@media (max-width: 600px) {
  .intraday-canvas {
    height: 320px;
  }

  .quote-table td {
    padding: 6px 8px;
  }
}

.no-data {
  text-align: center;
  color: #919191;
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
  color: #909090;
  font-size: 13px;
}

.quote-table td:not(.tlabel) {
  color: #ddd;
  font-weight: bold;
}

.up {
  color: var(--up);
  font-weight: bold;
}
.down {
  color: var(--down);
  font-weight: bold;
}

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
