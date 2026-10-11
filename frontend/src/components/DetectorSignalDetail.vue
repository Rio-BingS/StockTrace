<script setup>
// 信号详情：落库的维度判定快照 + 各微变维度近 20 日曲线（详情请求时按最新序列重算）。
// 曲线与判定快照分离：快照是信号当日的事实（不可变），曲线是活数据（能看到信号之后怎么走）。
import { ref, computed, onMounted, watch } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, MarkLineComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { detectorApi } from '../api/stocks.js'
import { errorMessage } from '../utils/errorMessage.js'
import { SEVERITY_LABEL, STATUS_LABEL, dirText } from '../constants/labels.js'
import { axisLabel, tooltip } from '../charts/theme.js'
import { fmtFixed } from '../utils/number.js'
import { directionalExcess, fmtRate, fmtReturn, signClass } from '../utils/detectorReview.js'

use([LineChart, GridComponent, TooltipComponent, MarkLineComponent, CanvasRenderer])

const props = defineProps({
  signalId: { type: Number, required: true },
})

const loading = ref(false)
const error = ref('')
const detail = ref(null)

const signal = computed(() => detail.value?.signal || {})
const dims = computed(() => {
  const dimensions = signal.value.dimensions || {}
  const series = detail.value?.dimension_series || {}
  // 与后端注册表同序的展示顺序无关紧要，这里按 key 排序保证稳定
  return Object.keys(dimensions)
    .sort()
    .map((key) => ({
      key,
      ...(dimensions[key] || {}),
      points: series[key] || [],
    }))
})

function chartOption(dim) {
  const points = dim.points.filter((p) => p.value != null)
  if (!points.length) return null
  return {
    backgroundColor: 'transparent',
    tooltip: tooltip({
      trigger: 'axis',
      textStyle: { fontSize: 11 },
      valueFormatter: (v) => fmtFixed(v, 1),
    }),
    grid: { left: 6, right: 6, top: 8, bottom: 4, containLabel: true },
    xAxis: {
      type: 'category',
      data: dim.points.map((p) => p.date.slice(5)),
      axisLine: { lineStyle: { color: '#2a3a5c' } },
      axisLabel: axisLabel({ fontSize: 10 }),
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: '#1a2744' } },
      axisLabel: axisLabel({ fontSize: 10 }),
    },
    series: [
      {
        type: 'line',
        data: dim.points.map((p) => p.value),
        symbol: 'none',
        lineStyle: { color: '#79c7ff', width: 1.5 },
        markLine: {
          // 零轴：分值符号即微变方向（正=向上、负=向下）
          silent: true,
          symbol: 'none',
          label: { show: false },
          lineStyle: { color: '#444', type: 'solid', width: 1 },
          data: [{ yAxis: 0 }],
        },
      },
    ],
  }
}

// 每个维度的图表配置只在数据变化时算一次；模板里原来 filter + 绑定各调用一次，
// 每次渲染都重建全部 option，触发图表整体重绘（FEX-07）
const dimCharts = computed(() =>
  dims.value.map((dim) => ({ dim, option: chartOption(dim) })).filter((c) => c.option),
)

async function load() {
  loading.value = true
  error.value = ''
  try {
    detail.value = (await detectorApi.getSignalDetail(props.signalId)).data
  } catch (e) {
    error.value = errorMessage(e, '加载信号详情失败')
  } finally {
    loading.value = false
  }
}

watch(() => props.signalId, load)
onMounted(load)
</script>

<template>
  <section class="card">
    <div v-if="error" class="error-box">{{ error }}</div>
    <div v-else-if="loading" class="empty">加载中...</div>
    <template v-else-if="signal.id">
      <div class="head">
        <h2>
          {{ signal.target_name }}
          <small>{{ signal.target_kind_display }} · {{ signal.target_code }}</small>
        </h2>
        <div class="badges">
          <span class="dir" :class="signal.direction"
            >{{ dirText(signal.direction) }} {{ signal.trade_date }}</span
          >
          <span class="badge">强度 {{ signal.score }}</span>
          <span class="badge">周期 {{ signal.cycles }}</span>
          <span class="badge sev" :class="signal.severity">{{
            SEVERITY_LABEL[signal.severity]
          }}</span>
          <span class="badge status" :class="signal.status">{{
            STATUS_LABEL[signal.status] || signal.status
          }}</span>
        </div>
      </div>

      <div class="dims">
        <div v-for="dim in dims" :key="dim.key" class="dim-chip" :class="{ off: !dim.available }">
          <b>{{ dim.name }}</b>
          <span :class="dim.direction">{{
            dim.available ? dirText(dim.direction) : '不可用'
          }}</span>
          <small v-if="dim.available && dim.note">{{ dim.note }}</small>
          <small v-else-if="!dim.available">{{ dim.note }}</small>
        </div>
        <div
          v-for="dim in detail?.disabled_dims || []"
          :key="'off-' + dim.key"
          class="dim-chip disabled"
        >
          <b>{{ dim.name }}</b>
          <span>未启用</span>
          <small>{{ dim.disabled_reason }}</small>
        </div>
      </div>

      <div v-if="signal.review_hit != null" class="review-line">
        回看结果：{{ signal.review_hit ? '已兑现' : '未兑现' }}
        <template v-if="signal.hit_date">（{{ signal.hit_date }} 达成）</template>
        <!-- v2 口径的明细（#12）；旧口径信号没有这些字段，整段不显示 -->
        <template v-if="signal.review_version">
          · 阈值 {{ fmtFixed(signal.review_threshold, 2) }}% · 同标的基线
          {{ fmtRate(signal.review_baseline) }} · 5 日收益 {{ fmtReturn(signal.fwd_return) }}
          <template v-if="signal.excess_return != null">
            · 相对沪深300（按方向）
            <b :class="signClass(directionalExcess(signal))">{{
              fmtReturn(directionalExcess(signal))
            }}</b>
          </template>
        </template>
      </div>

      <div class="charts">
        <div v-for="{ dim, option } in dimCharts" :key="'c-' + dim.key" class="chart-cell">
          <label
            >{{ dim.name }}（{{
              dim.direction === 'up' ? '向上' : dim.direction === 'down' ? '向下' : '未投票'
            }}）</label
          >
          <v-chart :option="option" autoresize style="height: 130px; width: 100%" />
        </div>
      </div>
      <p class="hint">
        曲线为该维度归一化微变分值（-100~100，正=向上、负=向下），按最新序列重算供观察后续演化。
      </p>
    </template>
  </section>
</template>

<style scoped>
.card {
  background: #111d34;
  border: 1px solid #213251;
  border-radius: 10px;
  padding: 12px 14px;
  margin-bottom: 10px;
}
.head {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 10px;
}
.head h2 {
  margin: 0;
  font-size: 15px;
  color: #e6ecf4;
}
.head h2 small {
  color: #8691a4;
  font-size: 11px;
  font-weight: 400;
  margin-left: 6px;
}
.badges {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.badge,
.dir {
  border-radius: 4px;
  padding: 3px 8px;
  font-size: 11px;
  background: #16213e;
  color: #8f9bb2;
}
.dir.up {
  color: var(--up);
  background: var(--up-bg);
}
.dir.down {
  color: var(--down);
  background: var(--down-bg);
}
.badge.sev.high {
  color: var(--danger);
  background: var(--danger-bg);
}
.badge.sev.medium {
  color: #ffbd6b;
  background: #382711;
}
.badge.sev.low {
  color: #8f9bb2;
}
.badge.status.active {
  color: var(--ok);
  background: var(--ok-bg);
}
.badge.status.confirmed {
  color: #79c7ff;
  background: #122736;
}
.badge.status.missed {
  color: var(--danger);
  background: var(--danger-bg);
}

.dims {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
}
.dim-chip {
  background: #0d1729;
  border: 1px solid #213251;
  border-radius: 7px;
  padding: 7px 10px;
  font-size: 11px;
  min-width: 130px;
}
.dim-chip b {
  display: block;
  color: #dce4f2;
  margin-bottom: 3px;
}
.dim-chip span {
  color: #8f9bb2;
  margin-right: 6px;
}
.dim-chip span.up {
  color: var(--up);
}
.dim-chip span.down {
  color: var(--down);
}
.dim-chip small {
  color: #8690a4;
  display: block;
  line-height: 1.4;
  max-width: 220px;
  white-space: normal;
}
.dim-chip.off {
  opacity: 0.65;
}
.dim-chip.disabled {
  border-style: dashed;
}

.review-line {
  background: #122736;
  border: 1px solid #2e4a5e;
  color: #8fc6d6;
  border-radius: 7px;
  padding: 8px 10px;
  margin-bottom: 10px;
  font-size: 12px;
}
.review-line b.up {
  color: var(--up);
}
.review-line b.down {
  color: var(--down);
}

.charts {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 10px;
}
.chart-cell {
  background: #0d1729;
  border: 1px solid #1d2a43;
  border-radius: 8px;
  padding: 8px 10px;
}
.chart-cell label {
  display: block;
  color: #8391a9;
  font-size: 11px;
  margin-bottom: 2px;
}
.hint {
  margin: 9px 0 0;
  color: #8690a4;
  font-size: 11px;
  line-height: 1.6;
}
.error-box {
  background: var(--danger-bg);
  color: var(--danger);
  padding: 10px;
  border-radius: 7px;
}
.empty {
  text-align: center;
  padding: 36px 20px;
  color: #8690a4;
}
</style>
