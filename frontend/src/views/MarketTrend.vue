<script setup>
import { ref, computed, onMounted } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import {
  TooltipComponent,
  GridComponent,
  LegendComponent,
  DataZoomComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import MarketSubNav from '../components/MarketSubNav.vue'
import PeriodPicker from '../components/PeriodPicker.vue'
import { marketApi } from '../api/stocks.js'
import { formatPct, pctClass } from '../utils/format.js'
import { errorMessage } from '../utils/errorMessage.js'

use([
  LineChart,
  TooltipComponent,
  GridComponent,
  LegendComponent,
  DataZoomComponent,
  CanvasRenderer,
])

const COLORS = ['#e94560', '#4fc3f7', '#ffb74d', '#81c784', '#ba68c8']

const PERIOD_OPTIONS = [
  ['1w', '近1周'], ['1m', '近1月'], ['3m', '近3月'],
  ['6m', '近半年'], ['1y', '近1年'], ['ytd', '今年以来'], ['custom', '自定义'],
]
const PERIOD_DAYS = { '1w': 5, '1m': 22, '3m': 66, '6m': 130, '1y': 260 }

const loading = ref(false)
const error = ref('')
const data = ref(null)
const period = ref('3m')
const customStart = ref('')
const customEnd = ref('')

const seriesList = computed(() => data.value?.series || [])
const rangeLabel = computed(() => {
  if (period.value === 'custom') {
    return data.value?.start ? `${data.value.start} ~ ${data.value.end || ''}` : '自定义区间'
  }
  return `近 ${data.value?.days || PERIOD_DAYS[period.value] || 66} 交易日`
})

const chartOption = computed(() => {
  const list = seriesList.value
  if (!list.length) return {}

  // 对齐日期并集
  const dateSet = new Set()
  list.forEach(s => (s.items || []).forEach(i => dateSet.add(i.date)))
  const dates = Array.from(dateSet).sort()

  const chartSeries = list.map((s, idx) => {
    const map = Object.fromEntries((s.items || []).map(i => [i.date, i.norm_pct]))
    return {
      name: s.name,
      type: 'line',
      showSymbol: false,
      smooth: true,
      data: dates.map(d => map[d] ?? null),
      itemStyle: { color: COLORS[idx % COLORS.length] },
      lineStyle: { width: 2 },
    }
  })

  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#1a1a2e',
      borderColor: '#333',
      textStyle: { color: '#ddd', fontSize: 12 },
      valueFormatter: (v) => (v == null ? '-' : `${Number(v).toFixed(2)}%`),
    },
    legend: {
      data: list.map(s => s.name),
      textStyle: { color: '#ccc', fontSize: 12 },
      top: 0,
    },
    grid: { left: '8%', right: '4%', top: 48, bottom: 60 },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { type: 'slider', start: 0, end: 100, height: 18, bottom: 8, textStyle: { color: '#888' } },
    ],
    xAxis: {
      type: 'category',
      data: dates,
      axisLabel: { color: '#888', fontSize: 11 },
      axisLine: { lineStyle: { color: '#444' } },
    },
    yAxis: {
      type: 'value',
      name: '相对涨跌%',
      nameTextStyle: { color: '#888', fontSize: 11 },
      axisLabel: {
        color: '#888',
        fontSize: 11,
        formatter: (v) => `${v}%`,
      },
      splitLine: { lineStyle: { color: '#1a2744' } },
    },
    series: chartSeries,
  }
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const params = period.value === 'custom'
      ? { start: customStart.value, end: customEnd.value }
      : { period: period.value }
    const res = await marketApi.getTrend(params)
    data.value = res.data
  } catch (e) {
    console.error(e)
    error.value = errorMessage(e, '加载走势失败')
  } finally {
    loading.value = false
  }
}

function setPeriod(value) {
  if (period.value === value) return
  period.value = value
  if (value !== 'custom') load()
}

function applyCustom() {
  if (!customStart.value && !customEnd.value) return
  load()
}

onMounted(load)
</script>

<template>
  <div class="page">
    <MarketSubNav />
    <div class="page-header">
      <div>
        <h1>全市场走势</h1>
        <p class="sub">
          {{ rangeLabel }}归一化对比（首日=0%）
          <span v-if="data?.updated_at"> · {{ data.updated_at }}</span>
        </p>
      </div>
      <button class="btn" @click="load" :disabled="loading">
        {{ loading ? '加载中...' : '刷新' }}
      </button>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>

    <div class="range-bar">
      <PeriodPicker
        :model-value="period"
        :options="PERIOD_OPTIONS"
        :loading="loading"
        @update:model-value="setPeriod"
      />
      <div v-if="period === 'custom'" class="custom-range">
        <input v-model="customStart" type="date" aria-label="开始日期" />
        <span>至</span>
        <input v-model="customEnd" type="date" aria-label="结束日期" />
        <button class="apply-btn" :disabled="loading" @click="applyCustom">查询</button>
      </div>
    </div>

    <section class="section">
      <v-chart
        v-if="chartOption.series?.length"
        :option="chartOption"
        autoresize
        style="height: 420px; width: 100%"
      />
      <div v-else-if="!loading" class="empty">
        {{ data?.message || '暂无走势数据' }}
      </div>
    </section>

    <section class="section" v-if="seriesList.length">
      <h2>区间涨跌幅</h2>
      <div class="stat-grid">
        <div v-for="s in seriesList" :key="s.code" class="stat-card">
          <div class="name">{{ s.name }}</div>
          <div class="code">{{ s.code }}</div>
          <div class="pct" :class="pctClass(s.period_change_pct)">
            {{ formatPct(s.period_change_pct) }}
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.page { color: #ddd; }
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 16px;
}
.page-header h1 { margin: 0; font-size: 22px; color: #eee; }
.sub { color: #888; font-size: 13px; margin-top: 4px; }
.btn {
  padding: 8px 16px;
  border: none;
  border-radius: 6px;
  background: #e94560;
  color: #fff;
  cursor: pointer;
}
.btn:disabled { opacity: 0.6; }
.range-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.custom-range { display: flex; align-items: center; gap: 6px; font-size: 12px; color: #9eabb8; }
.custom-range input { background: #121b24; border: 1px solid #2b3947; color: #ddd; border-radius: 5px; padding: 4px 7px; color-scheme: dark; }
.apply-btn { padding: 5px 12px; border: 1px solid #286391; background: #174673; color: #fff; border-radius: 5px; cursor: pointer; }
.apply-btn:disabled { opacity: .5; }
.error-box {
  background: #3a1520;
  color: #ff8a9a;
  padding: 12px;
  border-radius: 8px;
  margin-bottom: 12px;
}
.section {
  background: #16213e;
  border-radius: 8px;
  padding: 16px;
  margin-bottom: 16px;
}
.section h2 {
  margin: 0 0 12px;
  font-size: 15px;
  color: #eee;
}
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 10px;
}
.stat-card {
  background: #1a1a2e;
  border-radius: 8px;
  padding: 12px;
  border: 1px solid #1a2744;
}
.name { font-weight: 600; color: #eee; font-size: 14px; }
.code { color: #666; font-size: 12px; margin: 2px 0 8px; }
.pct { font-size: 20px; font-weight: 700; }
.empty {
  text-align: center;
  color: #666;
  padding: 40px;
}
@media (max-width: 640px) {
  .page-header h1 { font-size: 18px; }
}
</style>
