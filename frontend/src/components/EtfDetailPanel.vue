<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { LineChart, BarChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { marketApi } from '../api/stocks.js'
import { formatNum, formatPct, formatSigned, pctClass } from '../utils/format.js'
import { formatAmount, formatShare } from '../utils/marketFormat.js'
import { ACCENT } from '../constants/colors.js'
import { tooltip } from '../charts/theme.js'
import MarketDataStatus from './MarketDataStatus.vue'
import { errorMessage, isCanceled } from '../utils/errorMessage.js'
import { createRequestGuard } from '../utils/requestGuard.js'
import { adjustHint, adjustLabel, priceSeriesName } from '../utils/adjust.js'

use([
  LineChart,
  BarChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  CanvasRenderer,
])

const props = defineProps({ code: { type: String, default: '' } })
const loading = ref(false)
const error = ref('')
const data = ref(null)
const range = ref('3m')

const RANGE_OPTIONS = [
  ['1w', '1周'],
  ['1m', '1月'],
  ['3m', '3月'],
  ['6m', '半年'],
  ['1y', '1年'],
  ['custom', '自定义'],
]
const _today = new Date()
// 本地日期 YYYY-MM-DD；toISOString 是 UTC，北京时间 0-8 点会差一天（DAT-18）
const _localDate = (d) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
const _ago = (days) => {
  const d = new Date(_today)
  d.setDate(d.getDate() - days)
  return _localDate(d)
}
const customStart = ref(_ago(30))
const customEnd = ref(_localDate(_today))

const quote = computed(() => data.value?.quote || {})
const performance = computed(() => data.value?.price_performance || {})
const history = computed(() => data.value?.history?.items || [])
const stats = computed(() => data.value?.history?.stats || null)
// 价格历史的复权口径（DAT-14：东财前复权 qfq、新浪备源不复权 none）：
// 图例与区间行显示中文标签，区间统计与 5/20/60 日涨跌悬停提示口径说明
const adjust = computed(() => data.value?.history?.adjust)
const adjustText = computed(() => adjustLabel(adjust.value))
const adjustTitle = computed(() => adjustHint(adjust.value))
const priceName = computed(() => priceSeriesName(adjust.value))
const shareHistory = computed(() => data.value?.share_history || null)
const holders = computed(() => data.value?.holder_structure || null)
const chartOption = computed(() => {
  if (!history.value.length) return {}
  const shareMap = {}
  for (const it of shareHistory.value?.items || []) shareMap[it.date] = it.share
  const hasShare =
    !!shareHistory.value?.available && history.value.some((i) => shareMap[i.date] != null)
  const gridRight = hasShare ? 56 : 14
  const yAxes = [
    {
      type: 'value',
      scale: true,
      axisLabel: { color: '#6f7d96' },
      splitLine: { lineStyle: { color: '#1b2943' } },
    },
    { type: 'value', gridIndex: 1, axisLabel: { show: false }, splitLine: { show: false } },
  ]
  const series = [
    {
      name: priceName.value,
      type: 'line',
      showSymbol: false,
      smooth: true,
      data: history.value.map((i) => i.close),
      itemStyle: { color: ACCENT },
      lineStyle: { width: 2 },
    },
    {
      name: '成交额',
      type: 'bar',
      xAxisIndex: 1,
      yAxisIndex: 1,
      data: history.value.map((i) => i.turnover),
      itemStyle: { color: '#315e8c' },
    },
  ]
  if (hasShare) {
    yAxes.push({
      type: 'value',
      scale: true,
      axisLabel: { color: '#9a8a5c', formatter: (v) => `${(v / 1e8).toFixed(1)}亿` },
      splitLine: { show: false },
    })
    series.push({
      name: '份额',
      type: 'line',
      yAxisIndex: 2,
      showSymbol: false,
      data: history.value.map((i) => shareMap[i.date] ?? null),
      itemStyle: { color: '#d6a13c' },
      lineStyle: { width: 1.5, type: 'dashed' },
    })
  }
  return {
    backgroundColor: 'transparent',
    tooltip: tooltip({ trigger: 'axis' }),
    legend: {
      data: hasShare ? [priceName.value, '成交额', '份额'] : [priceName.value, '成交额'],
      textStyle: { color: '#8490a8' },
      top: 0,
    },
    grid: [
      { left: 45, right: gridRight, top: 34, height: '54%' },
      { left: 45, right: gridRight, top: '73%', height: '16%' },
    ],
    xAxis: [
      {
        type: 'category',
        data: history.value.map((i) => i.date),
        axisLabel: { color: '#6f7d96', hideOverlap: true, showMinLabel: true, showMaxLabel: true },
        axisLine: { lineStyle: { color: '#2b3956' } },
      },
      {
        type: 'category',
        gridIndex: 1,
        data: history.value.map((i) => i.date),
        axisLabel: { show: false },
        axisLine: { lineStyle: { color: '#2b3956' } },
      },
    ],
    yAxis: yAxes,
    series,
  }
})

// 快速切换 ETF/区间时取消旧请求，避免旧 ETF 的详情覆盖新选中的（FEF-06）
const guard = createRequestGuard()

async function load() {
  if (!props.code) {
    guard.abort()
    data.value = null
    loading.value = false
    return
  }
  const params = { range: range.value }
  if (range.value === 'custom') {
    if (!customStart.value || !customEnd.value) {
      guard.abort()
      loading.value = false
      return
    }
    params.start_date = customStart.value
    params.end_date = customEnd.value
  }
  const req = guard.next()
  loading.value = true
  error.value = ''
  try {
    const res = await marketApi.getEtfDetail(props.code, params, { signal: req.signal })
    if (!req.isLatest()) return
    data.value = res.data
  } catch (e) {
    if (!req.isLatest() || isCanceled(e)) return
    error.value = errorMessage(e, 'ETF 详情加载失败')
    data.value = null
  } finally {
    if (req.isLatest()) loading.value = false
  }
}

watch(() => props.code, load, { immediate: true })
watch(range, load)
onUnmounted(() => guard.abort())
</script>

<template>
  <aside class="detail-panel">
    <div v-if="!code" class="placeholder">选择左侧 ETF 查看行情与价格历史</div>
    <div v-else-if="loading" class="placeholder">详情加载中...</div>
    <div v-else-if="error" class="error">{{ error }}</div>
    <template v-else-if="data">
      <header>
        <div>
          <h2>{{ data.instrument?.name }}</h2>
          <span>{{ data.instrument?.code }} · {{ data.instrument?.exchange || '-' }}</span>
        </div>
        <div class="price">
          <strong>{{ formatNum(quote.price) }}</strong
          ><span :class="pctClass(quote.change_pct)">{{ formatPct(quote.change_pct) }}</span>
        </div>
      </header>
      <div class="metrics">
        <div>
          <label>最新份额</label><b>{{ formatShare(quote.share) }}</b>
        </div>
        <div>
          <label>总市值</label><b>{{ formatAmount(quote.market_cap) }}</b>
        </div>
        <div>
          <label>成交额</label><b>{{ formatAmount(quote.turnover) }}</b>
        </div>
        <div>
          <label>主力净流入</label
          ><b :class="pctClass(quote.main_net)">{{
            formatAmount(quote.main_net, { signed: true })
          }}</b>
        </div>
        <div>
          <label>IOPV</label><b>{{ formatNum(quote.iopv, 3) }}</b>
        </div>
        <div>
          <label>折溢价</label
          ><b :class="pctClass(quote.discount_rate)">{{ formatPct(quote.discount_rate) }}</b>
        </div>
      </div>
      <div class="range-tabs">
        <button
          v-for="r in RANGE_OPTIONS"
          :key="r[0]"
          :class="{ active: range === r[0] }"
          :aria-pressed="range === r[0] ? 'true' : 'false'"
          @click="range = r[0]"
        >
          {{ r[1] }}
        </button>
      </div>
      <div v-if="range === 'custom'" class="custom-range">
        <input v-model="customStart" type="date" :max="customEnd" />
        <span>至</span>
        <input v-model="customEnd" type="date" :min="customStart" :max="customEnd" />
        <button class="apply" :disabled="!customStart || !customEnd || loading" @click="load">
          查询
        </button>
      </div>
      <v-chart
        v-if="history.length && chartOption.series"
        :option="chartOption"
        autoresize
        class="chart"
      />
      <p v-if="history.length" class="history-range">
        数据区间 {{ data.history.start_date }} ~ {{ data.history.end_date }} ·
        {{ data.history.count }} 个交易日
        <template v-if="adjustText">
          · 价格口径
          <span class="adjust-tag" :class="adjust" :title="adjustTitle">{{ adjustText }}</span>
        </template>
      </p>
      <p v-if="shareHistory?.available" class="history-range">
        虚线为份额曲线（右轴 · 上交所历史/本站快照）
      </p>
      <p v-else-if="shareHistory?.message" class="history-range">{{ shareHistory.message }}</p>
      <div v-else class="history-empty">
        <b>价格历史暂不可用</b>
        <p>当前行情仍可查看；历史数据源恢复后可重新刷新。</p>
      </div>

      <!-- 持有人结构（定报，半年频）：汇金等长线配置盘的间接观察 -->
      <div v-if="holders?.available && holders.items?.length" class="holders">
        <div class="holders-head">
          <b>持有人结构（基金定报）</b>
          <span>机构占比最新 {{ holders.items[0].institution_pct ?? '-' }}%</span>
          <span
            v-if="
              holders.items.length > 1 &&
              holders.items[0].institution_pct != null &&
              holders.items[1].institution_pct != null
            "
          >
            环比上一期
            {{
              formatSigned(holders.items[0].institution_pct - holders.items[1].institution_pct, 1)
            }}pct
          </span>
        </div>
        <table class="holders-table">
          <thead>
            <tr>
              <th>公告期</th>
              <th>机构</th>
              <th>个人</th>
              <th>内部</th>
              <th>总份额</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="h in holders.items.slice(0, 4)" :key="h.announce_date">
              <td>{{ h.announce_date }}</td>
              <td>{{ h.institution_pct ?? '-' }}%</td>
              <td>{{ h.individual_pct ?? '-' }}%</td>
              <td>{{ h.internal_pct ?? '-' }}%</td>
              <td>{{ formatShare(h.total_shares) }}</td>
            </tr>
          </tbody>
        </table>
        <p class="holders-note">{{ holders.meta?.disclaimer }}</p>
      </div>
      <div v-if="stats" class="win-stats" :title="adjustTitle || undefined">
        <div>
          <span>区间涨跌</span
          ><b :class="pctClass(stats.change_pct)">{{ formatPct(stats.change_pct) }}</b>
        </div>
        <div>
          <span>区间最高</span><b>{{ formatNum(stats.high) }}</b>
        </div>
        <div>
          <span>区间最低</span><b>{{ formatNum(stats.low) }}</b>
        </div>
        <div>
          <span>日均成交额</span><b>{{ formatAmount(stats.avg_turnover) }}</b>
        </div>
        <div>
          <span>交易日</span><b>{{ stats.count }}</b>
        </div>
      </div>
      <div class="returns" :title="adjustTitle || undefined">
        <div>
          <span>5日</span
          ><b :class="pctClass(performance.return_5d)">{{ formatPct(performance.return_5d) }}</b>
        </div>
        <div>
          <span>20日</span
          ><b :class="pctClass(performance.return_20d)">{{ formatPct(performance.return_20d) }}</b>
        </div>
        <div>
          <span>60日</span
          ><b :class="pctClass(performance.return_60d)">{{ formatPct(performance.return_60d) }}</b>
        </div>
      </div>
      <div class="share-note">
        <b
          >份额变化{{
            data.share_metrics?.availability === 'daily_snapshot' ? '（日度快照）' : ''
          }}</b
        >
        <p>{{ data.share_metrics?.message }}</p>
        <div v-if="data.share_metrics?.availability === 'daily_snapshot'" class="share-chgs">
          <span
            >1日
            <b :class="pctClass(data.share_metrics.share_chg_1d)">{{
              formatShare(data.share_metrics.share_chg_1d, { signed: true })
            }}</b></span
          >
          <span
            >5日
            <b :class="pctClass(data.share_metrics.share_chg_5d)">{{
              formatShare(data.share_metrics.share_chg_5d, { signed: true })
            }}</b></span
          >
          <span
            >20日
            <b :class="pctClass(data.share_metrics.share_chg_20d)">{{
              formatShare(data.share_metrics.share_chg_20d, { signed: true })
            }}</b></span
          >
        </div>
      </div>
      <MarketDataStatus v-if="data.history?.meta" :meta="data.history.meta" />
      <MarketDataStatus :meta="data.meta" />
    </template>
  </aside>
</template>

<style scoped>
.detail-panel {
  min-height: 520px;
  background: #111d34;
  border: 1px solid #213251;
  border-radius: 10px;
  padding: 14px;
  color: #dce4f2;
}
.placeholder,
.error {
  display: grid;
  place-items: center;
  min-height: 420px;
  color: #8690a4;
  text-align: center;
}
.error {
  color: var(--danger);
}
header {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  border-bottom: 1px solid #213251;
  padding-bottom: 12px;
}
h2 {
  margin: 0;
  font-size: 18px;
}
header span {
  color: #8391a7;
  font-size: 12px;
}
.price {
  text-align: right;
}
.price strong {
  display: block;
  font-size: 22px;
}
.price span {
  font-size: 14px;
}
.metrics {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
  margin: 12px 0;
}
.metrics div {
  background: #0c172a;
  border-radius: 7px;
  padding: 9px;
}
.metrics label {
  display: block;
  color: #8691a4;
  font-size: 11px;
}
.metrics b {
  display: block;
  margin-top: 4px;
  font-size: 13px;
}
.range-tabs {
  display: flex;
  gap: 5px;
}
.range-tabs button {
  border: 1px solid #273a5c;
  background: #101a2c;
  color: #8391a7;
  border-radius: 5px;
  padding: 4px 9px;
  cursor: pointer;
}
.range-tabs button.active {
  background: #174673;
  color: #fff;
}
.custom-range {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 7px;
  color: #8391a7;
  font-size: 12px;
}
.custom-range input {
  background: #0c172a;
  border: 1px solid #273a5c;
  color: #dce4f2;
  border-radius: 5px;
  padding: 3px 6px;
  color-scheme: dark;
  font-size: 12px;
}
.custom-range .apply {
  border: 1px solid #273a5c;
  background: #174673;
  color: #fff;
  border-radius: 5px;
  padding: 4px 10px;
  cursor: pointer;
}
.custom-range .apply:disabled {
  opacity: 0.5;
  cursor: default;
}
.win-stats {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 6px;
  margin: 10px 0;
}
.win-stats div {
  background: #0c172a;
  padding: 8px 4px;
  text-align: center;
  border-radius: 6px;
}
.win-stats span {
  color: #8691a4;
  font-size: 11px;
  display: block;
}
.win-stats b {
  font-size: 12px;
}
.chart {
  width: 100%;
  height: 300px;
}
.history-range {
  margin: 2px 0 8px;
  color: #8691a4;
  font-size: 11px;
  text-align: center;
}
.adjust-tag {
  padding: 0 5px;
  border: 1px solid #3a5a86;
  border-radius: 4px;
  color: #9cc6ff;
  cursor: help;
}
/* 不复权：除权日会跳空，用警示色提醒 */
.adjust-tag.none {
  border-color: #7a5a2a;
  color: #f0bd71;
}
.history-empty {
  min-height: 170px;
  margin: 10px 0;
  display: grid;
  place-content: center;
  text-align: center;
  border: 1px dashed #33445f;
  border-radius: 7px;
  color: #9ba8bd;
}
.history-empty p {
  margin: 5px 0 0;
  color: #8691a4;
  font-size: 12px;
}
.returns {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px;
}
.returns div {
  background: #0c172a;
  padding: 8px;
  text-align: center;
  border-radius: 6px;
}
.returns span {
  color: #8691a4;
  font-size: 11px;
  display: block;
}
.share-note {
  margin: 12px 0;
  padding: 10px;
  border: 1px dashed #33445f;
  border-radius: 7px;
  color: #a8b3c7;
  font-size: 12px;
}
.share-note p {
  margin: 4px 0 0;
  color: #8590a4;
  line-height: 1.5;
}
.share-chgs {
  display: flex;
  gap: 12px;
  margin-top: 8px;
}
.share-chgs span {
  color: #8590a4;
}
.share-chgs b {
  margin-left: 4px;
}
.holders {
  margin-top: 10px;
}
.holders-head {
  display: flex;
  gap: 14px;
  align-items: baseline;
  font-size: 13px;
  color: #ccc;
  margin-bottom: 6px;
}
.holders-head span {
  color: #909090;
  font-size: 12px;
}
.holders-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.holders-table th,
.holders-table td {
  padding: 4px 8px;
  text-align: right;
  border-bottom: 1px solid #26263a;
  color: #bbb;
}
.holders-table th:first-child,
.holders-table td:first-child {
  text-align: left;
}
.holders-note {
  margin-top: 6px;
  font-size: 11px;
  color: #919191;
}
</style>
