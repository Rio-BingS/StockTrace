<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
import {
  TooltipComponent,
  GridComponent,
  LegendComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import MarketSubNav from '../components/MarketSubNav.vue'
import PeriodPicker from '../components/PeriodPicker.vue'
import { marketApi } from '../api/stocks.js'
import { formatNum, formatPct, pctClass } from '../utils/format.js'
import { formatAmount, formatVolume } from '../utils/marketFormat.js'
import { errorMessage, isCanceled } from '../utils/errorMessage.js'
import { createRequestGuard } from '../utils/requestGuard.js'

use([BarChart, LineChart, TooltipComponent, GridComponent, LegendComponent, CanvasRenderer])

const loading = ref(false)
const detailLoading = ref(false)
const error = ref('')
const data = ref(null)
const tab = ref('stock') // stock | org
const stockCode = ref('')
const stockDetail = ref(null)
const detailError = ref('')

const byStock = computed(() => data.value?.by_stock || {})
const byOrg = computed(() => data.value?.by_institution || {})
const disclaimer = computed(() => data.value?.disclaimer || '')
const stockItems = computed(() => byStock.value.items || [])
const orgItems = computed(() => byOrg.value.items || [])

// ── 北向资金区间（/market/northbound/，未加载时回退 institutions 内嵌 30 日视图） ──
const NORTH_PERIODS = [
  ['1d', '当日'], ['3d', '3日'], ['5d', '5日'], ['1w', '近1周'],
  ['1m', '近1月'], ['3m', '近3月'], ['6m', '近半年'], ['1y', '近1年'], ['ytd', '今年以来'],
]
const northPeriod = ref('1m')
const northLoading = ref(false)
const northError = ref('')
const northData = ref(null)
const north = computed(() => northData.value || data.value?.northbound || {})
const northSummary = computed(() => northData.value?.summary || {})

// 切换区间/查询代码时取消旧请求、丢弃晚到的旧响应（FEF-06）
const northGuard = createRequestGuard()
const detailGuard = createRequestGuard()

async function loadNorth() {
  const req = northGuard.next()
  northLoading.value = true
  northError.value = ''
  try {
    const res = await marketApi.getNorthbound({ period: northPeriod.value }, { signal: req.signal })
    if (req.isLatest()) northData.value = res.data
  } catch (e) {
    if (!req.isLatest() || isCanceled(e)) return
    console.error(e)
    northError.value = errorMessage(e, '加载北向区间数据失败')
  } finally {
    if (req.isLatest()) northLoading.value = false
  }
}
function setNorthPeriod(p) {
  if (northPeriod.value === p) return
  northPeriod.value = p
  loadNorth()
}

const northChart = computed(() => {
  const items = north.value.items || []
  if (!items.length) return {}
  const dates = items.map(i => i.date)
  const nets = items.map(i => i.net_buy)
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#1a1a2e',
      borderColor: '#333',
      textStyle: { color: '#ddd', fontSize: 12 },
    },
    grid: { left: '8%', right: '4%', top: '14%', bottom: '18%' },
    xAxis: {
      type: 'category',
      data: dates,
      axisLabel: { color: '#888', fontSize: 11, rotate: 30 },
      axisLine: { lineStyle: { color: '#444' } },
    },
    yAxis: {
      type: 'value',
      name: '亿',
      nameTextStyle: { color: '#888' },
      axisLabel: { color: '#888', fontSize: 11 },
      splitLine: { lineStyle: { color: '#1a2744' } },
    },
    series: [{
      name: '北向净买额',
      type: 'bar',
      data: nets,
      itemStyle: {
        color: (p) => (p.value != null && p.value >= 0 ? '#e94560' : '#00c853'),
      },
    }],
  }
})

function formatChg(val) {
  if (val == null || Number.isNaN(Number(val))) return '-'
  const n = Number(val)
  const s = n > 0 ? `+${n}` : `${n}`
  return s
}

async function load(code = null) {
  loading.value = true
  error.value = ''
  try {
    const res = await marketApi.getInstitutions(code || undefined)
    data.value = res.data
    if (res.data?.stock_detail) {
      stockDetail.value = res.data.stock_detail
    }
  } catch (e) {
    console.error(e)
    error.value = errorMessage(e, '加载机构持仓失败')
  } finally {
    loading.value = false
  }
}

async function loadDetail() {
  const code = (stockCode.value || '').trim()
  if (!code) {
    detailError.value = '请输入 6 位股票代码'
    return
  }
  const req = detailGuard.next()
  detailLoading.value = true
  detailError.value = ''
  try {
    const res = await marketApi.getInstitutions(code, { signal: req.signal })
    if (!req.isLatest()) return
    stockDetail.value = res.data?.stock_detail || null
    if (!stockDetail.value?.available) {
      detailError.value = stockDetail.value?.message || '未查到该股机构数据'
    }
  } catch (e) {
    if (!req.isLatest() || isCanceled(e)) return
    console.error(e)
    detailError.value = errorMessage(e, '查询失败')
    stockDetail.value = null
  } finally {
    if (req.isLatest()) detailLoading.value = false
  }
}

onMounted(() => { load(); loadNorth() })
onUnmounted(() => { northGuard.abort(); detailGuard.abort() })
</script>

<template>
  <div class="page">
    <MarketSubNav />
    <div class="page-header">
      <div>
        <h1>机构持仓</h1>
        <p class="sub">
          季报机构持股 · 股东变动 · 北向资金
          <span v-if="data?.updated_at"> · {{ data.updated_at }}</span>
        </p>
      </div>
      <button class="btn" @click="load()" :disabled="loading">
        {{ loading ? '加载中...' : '刷新' }}
      </button>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>
    <div v-if="disclaimer" class="note">{{ disclaimer }}</div>

    <!-- 北向资金 -->
    <section class="section">
      <div class="section-head">
        <h2>北向资金（外资通道）</h2>
        <PeriodPicker
          :model-value="northPeriod"
          :options="NORTH_PERIODS"
          :loading="northLoading"
          @update:model-value="setNorthPeriod"
        />
      </div>
      <div v-if="northError" class="error-box sm">{{ northError }}</div>
      <p class="hint" v-if="!north.available">{{ north.message || '暂无北向序列' }}</p>
      <template v-else>
        <div class="north-summary" v-if="northSummary.days != null">
          <span>区间交易日 <b>{{ northSummary.days }}</b></span>
          <span>合计净买额 <b :class="pctClass(northSummary.total_net_buy)">
            {{ northSummary.total_net_buy != null ? `${northSummary.total_net_buy} 亿` : '-' }}
          </b></span>
          <span>净流入 <b class="up">{{ northSummary.inflow_days ?? '-' }} 天</b></span>
          <span>净流出 <b class="down">{{ northSummary.outflow_days ?? '-' }} 天</b></span>
          <span v-if="northData?.window?.label" class="muted">{{ northData.window.label }}</span>
        </div>
        <v-chart
          v-if="northChart.series"
          :option="northChart"
          autoresize
          style="height: 280px; width: 100%"
        />
      </template>
    </section>

    <!-- 个股查询 -->
    <section class="section">
      <h2>个股机构明细</h2>
      <div class="search-row">
        <input
          v-model="stockCode"
          class="code-input"
          maxlength="6"
          placeholder="股票代码，如 600519"
          @keyup.enter="loadDetail"
        />
        <button class="btn" @click="loadDetail" :disabled="detailLoading">
          {{ detailLoading ? '查询中...' : '查询' }}
        </button>
      </div>
      <div v-if="detailError" class="error-box sm">{{ detailError }}</div>
      <template v-if="stockDetail?.available">
        <p class="hint">
          {{ stockDetail.code }}
          <span v-if="stockDetail.quarter_label"> · 机构明细季报 {{ stockDetail.quarter_label }}</span>
        </p>
        <div class="detail-grid">
          <div class="table-wrap">
            <h3>机构持股明细</h3>
            <table class="data-table" v-if="stockDetail.institutions?.length">
              <thead>
                <tr>
                  <th>类型</th>
                  <th>机构</th>
                  <th>持股数</th>
                  <th>持股比例%</th>
                  <th>流通占比%</th>
                  <th>比例增幅</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(r, i) in stockDetail.institutions" :key="i">
                  <td>{{ r.type || '-' }}</td>
                  <td class="name">{{ r.inst_name || r.inst_code || '-' }}</td>
                  <td>{{ formatVolume(r.shares) }}</td>
                  <td>{{ formatNum(r.ratio) }}</td>
                  <td>{{ formatNum(r.float_ratio) }}</td>
                  <td :class="pctClass(r.ratio_chg)">{{ formatPct(r.ratio_chg) }}</td>
                </tr>
              </tbody>
            </table>
            <p v-else class="hint">无机构明细</p>
          </div>
          <div class="table-wrap">
            <h3>十大流通股东</h3>
            <table class="data-table" v-if="stockDetail.top_holders?.length">
              <thead>
                <tr>
                  <th>#</th>
                  <th>股东</th>
                  <th>持股数</th>
                  <th>比例%</th>
                  <th>性质</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(r, i) in stockDetail.top_holders" :key="i">
                  <td>{{ r.rank ?? i + 1 }}</td>
                  <td class="name">{{ r.name }}</td>
                  <td>{{ formatVolume(r.shares) }}</td>
                  <td>{{ formatNum(r.ratio) }}</td>
                  <td>{{ r.nature || '-' }}</td>
                </tr>
              </tbody>
            </table>
            <p v-else class="hint">无十大股东数据</p>
          </div>
        </div>
      </template>
    </section>

    <div class="seg">
      <button :class="{ active: tab === 'stock' }" @click="tab = 'stock'">
        按股票
        <span v-if="byStock.quarter_label" class="tag">{{ byStock.quarter_label }}</span>
      </button>
      <button :class="{ active: tab === 'org' }" @click="tab = 'org'">
        按机构
        <span v-if="byOrg.report_date" class="tag">{{ byOrg.report_date }}</span>
      </button>
    </div>

    <!-- 按股票 -->
    <section class="section" v-show="tab === 'stock'">
      <h2>机构持股变化（按股票）</h2>
      <p class="hint" v-if="!byStock.available">{{ byStock.message || '暂无数据' }}</p>
      <div class="table-wrap" v-else>
        <table class="data-table">
          <thead>
            <tr>
              <th>代码</th>
              <th>名称</th>
              <th>机构数</th>
              <th>机构数变化</th>
              <th>持股比例%</th>
              <th>比例增幅</th>
              <th>流通占比%</th>
              <th>流通占比增幅</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in stockItems" :key="row.code">
              <td>
                <button class="link-btn" @click="stockCode = row.code; loadDetail()">
                  {{ row.code }}
                </button>
              </td>
              <td class="name">{{ row.name }}</td>
              <td>{{ row.inst_count ?? '-' }}</td>
              <td :class="pctClass(row.inst_count_chg)">{{ formatChg(row.inst_count_chg) }}</td>
              <td>{{ formatNum(row.hold_ratio) }}</td>
              <td :class="pctClass(row.hold_ratio_chg)">{{ formatPct(row.hold_ratio_chg) }}</td>
              <td>{{ formatNum(row.float_ratio) }}</td>
              <td :class="pctClass(row.float_ratio_chg)">{{ formatPct(row.float_ratio_chg) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 按机构 -->
    <section class="section" v-show="tab === 'org'">
      <h2>机构股东变动（按机构）</h2>
      <p class="hint" v-if="byOrg.disclaimer">{{ byOrg.disclaimer }}</p>
      <p class="hint" v-if="!byOrg.available">{{ byOrg.message || '暂无数据' }}</p>
      <div class="table-wrap" v-else>
        <table class="data-table">
          <thead>
            <tr>
              <th>机构名称</th>
              <th>类型</th>
              <th>总持有</th>
              <th>新进</th>
              <th>增加</th>
              <th>减少</th>
              <th>流通市值</th>
              <th>样本个股</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, i) in orgItems" :key="i">
              <td class="name">{{ row.name }}</td>
              <td>{{ row.type || '-' }}</td>
              <td>{{ row.hold_count ?? '-' }}</td>
              <td class="up">{{ row.new_count ?? '-' }}</td>
              <td class="up">{{ row.increase_count ?? '-' }}</td>
              <td class="down">{{ row.decrease_count ?? '-' }}</td>
              <td>{{ formatAmount(row.market_value) }}</td>
              <td class="samples">
                <span
                  v-for="(s, j) in (row.sample_stocks || []).slice(0, 3)"
                  :key="j"
                  class="sample"
                >{{ s.name || s.code }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>

<style scoped>
.page {
  max-width: 1200px;
  margin: 0 auto;
  padding: 16px 20px 40px;
  color: #ddd;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 12px;
}

.page-header h1 {
  margin: 0;
  font-size: 22px;
  color: #fff;
}

.sub {
  margin: 6px 0 0;
  color: #888;
  font-size: 13px;
}

.btn {
  background: #0f3460;
  color: #fff;
  border: none;
  border-radius: 6px;
  padding: 8px 16px;
  cursor: pointer;
  font-size: 13px;
}

.btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.btn:hover:not(:disabled) {
  background: #1a4a7a;
}

.error-box {
  background: #3a1520;
  color: #ff8a9a;
  padding: 10px 14px;
  border-radius: 8px;
  margin-bottom: 12px;
  font-size: 13px;
}

.error-box.sm {
  margin-top: 8px;
}

.note {
  background: #1a2744;
  color: #aab;
  padding: 10px 14px;
  border-radius: 8px;
  margin-bottom: 16px;
  font-size: 12px;
  line-height: 1.5;
}

.section {
  background: #12122a;
  border: 1px solid #1a2744;
  border-radius: 10px;
  padding: 16px;
  margin-bottom: 16px;
}

.section h2 {
  margin: 0 0 12px;
  font-size: 16px;
  color: #eee;
}

.section h3 {
  margin: 0 0 8px;
  font-size: 14px;
  color: #ccc;
}

.hint {
  color: #888;
  font-size: 12px;
  margin: 0 0 10px;
}

.seg {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}

.seg button {
  background: #1a1a2e;
  color: #aaa;
  border: 1px solid #1a2744;
  border-radius: 6px;
  padding: 8px 14px;
  cursor: pointer;
  font-size: 13px;
}

.seg button.active {
  background: #0f3460;
  color: #fff;
  border-color: #1a4a7a;
}

.tag {
  margin-left: 6px;
  font-size: 11px;
  opacity: 0.8;
}

.search-row {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}

.code-input {
  background: #0d0d1a;
  border: 1px solid #1a2744;
  color: #eee;
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 14px;
  width: 180px;
}

.detail-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.table-wrap {
  overflow-x: auto;
}

.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.data-table th,
.data-table td {
  padding: 8px 10px;
  text-align: left;
  border-bottom: 1px solid #1a2744;
  white-space: nowrap;
}

.data-table th {
  color: #888;
  font-weight: 500;
}

.data-table td.name {
  max-width: 220px;
  white-space: normal;
  word-break: break-all;
}

.data-table .up { color: #e94560; }
.data-table .down { color: #00c853; }

.link-btn {
  background: none;
  border: none;
  color: #6eb6ff;
  cursor: pointer;
  padding: 0;
  font-size: inherit;
}

.link-btn:hover {
  text-decoration: underline;
}

.samples {
  white-space: normal;
  max-width: 200px;
}

.sample {
  display: inline-block;
  background: #1a2744;
  color: #bbb;
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
  margin: 2px 4px 2px 0;
}

@media (max-width: 900px) {
  .detail-grid {
    grid-template-columns: 1fr;
  }
}.section-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.section-head h2 {
  margin: 0;
}

.north-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  align-items: baseline;
  color: #99a;
  font-size: 12px;
  margin-bottom: 10px;
}

.north-summary b { color: #eee; font-size: 13px; }
.north-summary .muted { color: #667; }

</style>
