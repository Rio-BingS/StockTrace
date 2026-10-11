<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
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
import { errorMessage, isCanceled } from '../utils/errorMessage.js'
import { createRequestGuard } from '../utils/requestGuard.js'
import { ACCENT } from '../constants/colors.js'
// 图表 tooltip / 坐标轴配色统一取自 charts/theme.js（ARC-06）
import {
  TOOLTIP_STYLE,
  AXIS_LABEL_COLOR,
  AXIS_LINE_COLOR,
  SPLIT_LINE_COLOR,
} from '../charts/theme.js'

// 显式组件名：App.vue 的 KeepAlive include 按它匹配，不依赖文件名推断（FEF-15）
defineOptions({ name: 'MarketTrend' })

use([
  LineChart,
  TooltipComponent,
  GridComponent,
  LegendComponent,
  DataZoomComponent,
  CanvasRenderer,
])

// 多指数对比的系列色：首色用品牌蓝，不用涨跌红绿，避免被读成涨跌（FEX-01）
const COLORS = [ACCENT, '#ffb74d', '#ba68c8', '#4fc3f7', '#f06292']

const PERIOD_OPTIONS = [
  ['1w', '近1周'],
  ['1m', '近1月'],
  ['3m', '近3月'],
  ['6m', '近半年'],
  ['1y', '近1年'],
  ['ytd', '今年以来'],
  ['custom', '自定义'],
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
  list.forEach((s) => (s.items || []).forEach((i) => dateSet.add(i.date)))
  const dates = Array.from(dateSet).sort()

  const chartSeries = list.map((s, idx) => {
    const map = Object.fromEntries((s.items || []).map((i) => [i.date, i.norm_pct]))
    return {
      name: s.name,
      type: 'line',
      showSymbol: false,
      smooth: true,
      data: dates.map((d) => map[d] ?? null),
      itemStyle: { color: COLORS[idx % COLORS.length] },
      lineStyle: { width: 2 },
    }
  })

  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      ...TOOLTIP_STYLE,
      valueFormatter: (v) => (v == null ? '-' : `${Number(v).toFixed(2)}%`),
    },
    legend: {
      data: list.map((s) => s.name),
      textStyle: { color: '#ccc', fontSize: 12 },
      top: 0,
    },
    grid: { left: '8%', right: '4%', top: 48, bottom: 60 },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      {
        type: 'slider',
        start: 0,
        end: 100,
        height: 18,
        bottom: 8,
        textStyle: { color: AXIS_LABEL_COLOR },
      },
    ],
    xAxis: {
      type: 'category',
      data: dates,
      axisLabel: { color: AXIS_LABEL_COLOR, fontSize: 11 },
      axisLine: { lineStyle: { color: AXIS_LINE_COLOR } },
    },
    yAxis: {
      type: 'value',
      name: '相对涨跌%',
      nameTextStyle: { color: AXIS_LABEL_COLOR, fontSize: 11 },
      axisLabel: {
        color: AXIS_LABEL_COLOR,
        fontSize: 11,
        formatter: (v) => `${v}%`,
      },
      splitLine: { lineStyle: { color: SPLIT_LINE_COLOR } },
    },
    series: chartSeries,
  }
})

// 快速切换区间时取消旧请求、丢弃晚到的旧响应（FEF-06）
const guard = createRequestGuard()

async function load() {
  const req = guard.next()
  loading.value = true
  error.value = ''
  try {
    const params =
      period.value === 'custom'
        ? { start: customStart.value, end: customEnd.value }
        : { period: period.value }
    const res = await marketApi.getTrend(params, { signal: req.signal })
    if (!req.isLatest()) return
    data.value = res.data
  } catch (e) {
    if (!req.isLatest() || isCanceled(e)) return
    console.error(e)
    error.value = errorMessage(e, '加载走势失败')
  } finally {
    if (req.isLatest()) loading.value = false
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
onUnmounted(() => guard.abort())
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
.page {
  color: #ddd;
}
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 16px;
}
.page-header h1 {
  margin: 0;
  font-size: 22px;
  color: #eee;
}
.sub {
  color: #909090;
  font-size: 13px;
  margin-top: 4px;
}
.btn {
  padding: 8px 16px;
  border: none;
  border-radius: 6px;
  background: var(--accent-strong);
  color: #fff;
  cursor: pointer;
}
.btn:disabled {
  opacity: 0.6;
}
.range-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.custom-range {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #9eabb8;
}
.custom-range input {
  background: #121b24;
  border: 1px solid #2b3947;
  color: #ddd;
  border-radius: 5px;
  padding: 4px 7px;
  color-scheme: dark;
}
.apply-btn {
  padding: 5px 12px;
  border: 1px solid #286391;
  background: #174673;
  color: #fff;
  border-radius: 5px;
  cursor: pointer;
}
.apply-btn:disabled {
  opacity: 0.5;
}
.error-box {
  background: var(--danger-bg);
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
.name {
  font-weight: 600;
  color: #eee;
  font-size: 14px;
}
.code {
  color: #919191;
  font-size: 12px;
  margin: 2px 0 8px;
}
.pct {
  font-size: 20px;
  font-weight: 700;
}
.empty {
  text-align: center;
  color: #919191;
  padding: 40px;
}
@media (max-width: 640px) {
  .page-header h1 {
    font-size: 18px;
  }
}
</style>
