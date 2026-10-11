<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { RouterLink } from 'vue-router'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { BarChart, PieChart } from 'echarts/charts'
import {
  TooltipComponent,
  GridComponent,
  LegendComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { marketApi } from '../api/stocks.js'
import PeriodPicker from '../components/PeriodPicker.vue'
import { formatPct, formatNum, pctClass } from '../utils/format.js'
import { formatYi, formatYiAxis, formatTurnover, formatAmount } from '../utils/marketFormat.js'
import { errorMessage } from '../utils/errorMessage.js'

use([
  BarChart,
  PieChart,
  TooltipComponent,
  GridComponent,
  LegendComponent,
  CanvasRenderer,
])

const loading = ref(false)
const error = ref('')
const overview = ref(null)
let timer = null

const defaultModules = [
  { key: 'trend', title: '全市场走势', desc: '主要指数归一化对比', path: '/market/trend' },
  { key: 'sectors', title: '板块资金轮动', desc: '行业与概念资金排行', path: '/market/sectors' },
  { key: 'institutions', title: '机构持仓', desc: '国内外机构持股变化与北向', path: '/market/institutions' },
  { key: 'national-etf', title: '国家队相关 ETF 观察', desc: '非官方持仓的宽基与政策相关 ETF 观察名单', path: '/market/national-etf' },
  { key: 'etf-radar', title: 'ETF 份额雷达', desc: '份额与主力资金榜', path: '/market/etf-radar' },
]

const indices = computed(() => overview.value?.indices || [])
const valuations = computed(() => overview.value?.valuations || { available: false, items: [] })
const zt = computed(() => overview.value?.zt_sentiment || { available: false })
const margin = computed(() => overview.value?.margin_balance || { available: false })
const fund = computed(() => overview.value?.fund || {})
const activity = computed(() => fund.value.activity || {})
const hsgt = computed(() => fund.value.hsgt || [])
const mainHist = computed(() => flowData.value || fund.value.main_hist || { available: false, items: [], message: '' })

// ── 大盘资金流区间（/market/market-flow/，未加载时回退 overview 内嵌 30 日视图） ──
const FLOW_PERIODS = [
  ['1d', '当日'], ['3d', '3日'], ['5d', '5日'], ['1w', '近1周'],
  ['1m', '近1月'], ['3m', '近3月'], ['6m', '近半年'], ['ytd', '今年以来'],
]
const flowPeriod = ref('1m')
const flowLoading = ref(false)
const flowError = ref('')
const flowData = ref(null)
const flowSummary = computed(() => flowData.value?.summary || {})
const flowNote = computed(() => flowData.value?.note || '')

async function loadFlow() {
  flowLoading.value = true
  flowError.value = ''
  try {
    flowData.value = (await marketApi.getMarketFlow({ period: flowPeriod.value })).data
  } catch (e) {
    console.error(e)
    flowError.value = errorMessage(e, '加载大盘资金流区间失败')
  } finally {
    flowLoading.value = false
  }
}
function setFlowPeriod(p) {
  if (flowPeriod.value === p) return
  flowPeriod.value = p
  loadFlow()
}
const concept = computed(() => fund.value.concept || { available: false, inflow_top: [], outflow_top: [] })
const modules = computed(() => overview.value?.modules || defaultModules)
const northNet = computed(() => fund.value.northbound_net_buy)

const fundChartOption = computed(() => {
  const items = mainHist.value.items || []
  if (!items.length) return {}
  const dates = items.map(i => String(i.date).slice(0, 10))
  // 统一按"元"作图：东财 fflow / stock_market_fund_flow 主力净流入单位为元；
  // 北向兜底源（stock_hsgt_hist_em 净买额）单位为亿元，换算回元
  const factor = mainHist.value.source === 'eastmoney_hsgt_hist_north' ? 1e8 : 1
  const main = items.map(i => (i.main_net == null ? null : i.main_net * factor))
  const colors = main.map(v => (v != null && v >= 0 ? '#e94560' : '#00c853'))
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#1a1a2e',
      borderColor: '#333',
      textStyle: { color: '#ddd', fontSize: 12 },
      valueFormatter: (v) => formatAmount(v, { signed: true }),
    },
    grid: { left: '8%', right: '4%', top: '12%', bottom: '18%' },
    xAxis: {
      type: 'category',
      data: dates,
      axisLabel: { color: '#888', fontSize: 11, rotate: 30 },
      axisLine: { lineStyle: { color: '#444' } },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: '#888', fontSize: 11, formatter: formatYiAxis },
      splitLine: { lineStyle: { color: '#1a2744' } },
    },
    series: [{
      name: '主力净流入',
      type: 'bar',
      data: main,
      itemStyle: { color: (p) => colors[p.dataIndex] || '#888' },
    }],
  }
})

const activityChartOption = computed(() => {
  const a = activity.value
  const up = a.up ?? 0
  const down = a.down ?? 0
  const flat = a.flat ?? 0
  if (!up && !down && !flat) return {}
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'item', backgroundColor: '#1a1a2e', textStyle: { color: '#ddd' } },
    series: [{
      type: 'pie',
      radius: ['42%', '68%'],
      center: ['50%', '52%'],
      label: { color: '#ccc', fontSize: 12 },
      data: [
        { name: '上涨', value: up, itemStyle: { color: '#e94560' } },
        { name: '下跌', value: down, itemStyle: { color: '#00c853' } },
        { name: '平盘', value: flat, itemStyle: { color: '#888' } },
      ],
    }],
  }
})

async function loadMarket() {
  loading.value = true
  error.value = ''
  try {
    const res = await marketApi.getOverview()
    overview.value = res.data
  } catch (e) {
    console.error(e)
    error.value = errorMessage(e, '加载大盘数据失败')
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  loadMarket()
  loadFlow()
  timer = setInterval(loadMarket, 90000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div class="market-page">
    <div class="page-header">
      <div>
        <h1>市场数据</h1>
        <p class="sub" v-if="overview?.updated_at">更新时间 {{ overview.updated_at }}</p>
      </div>
      <button class="btn" @click="loadMarket" :disabled="loading">
        {{ loading ? '刷新中...' : '刷新' }}
      </button>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>

    <!-- 二级模块入口 -->
    <section class="section modules-section">
      <h2>专题模块</h2>
      <div class="module-grid">
        <RouterLink
          v-for="m in modules"
          :key="m.key"
          :to="m.path"
          class="module-card"
        >
          <div class="mod-title">{{ m.title }}</div>
          <div class="mod-desc">{{ m.desc }}</div>
          <div class="mod-go">进入 →</div>
        </RouterLink>
      </div>
    </section>

    <!-- 主要指数 -->
    <section class="section">
      <div class="section-head">
        <h2>主要指数</h2>
        <RouterLink class="link-more" to="/market/trend">走势对比 →</RouterLink>
      </div>
      <div class="index-grid">
        <div v-for="item in indices" :key="item.code" class="index-card">
          <div class="idx-name">{{ item.name }}</div>
          <div class="idx-code">{{ item.code }}</div>
          <div class="idx-price" :class="pctClass(item.change_pct)">
            {{ item.price != null ? Number(item.price).toFixed(2) : '-' }}
          </div>
          <div class="idx-change" :class="pctClass(item.change_pct)">
            <span>{{ formatNum(item.change) }}</span>
            <span>{{ formatPct(item.change_pct) }}</span>
          </div>
          <div class="idx-meta">
            <span>开 {{ formatNum(item.open) }}</span>
            <span>高 {{ formatNum(item.high) }}</span>
            <span>低 {{ formatNum(item.low) }}</span>
          </div>
          <div class="idx-meta muted">额 {{ formatTurnover(item.turnover) }}</div>
        </div>
        <div v-if="!indices.length && !loading" class="empty">暂无指数数据</div>
      </div>
    </section>

    <!-- 宽基指数估值分位 -->
    <section v-if="valuations.items?.length" class="section">
      <div class="section-head">
        <h2>指数估值（滚动市盈率）</h2>
        <span v-if="valuations.meta?.disclaimer" class="val-note">{{ valuations.meta.disclaimer }}</span>
      </div>
      <div class="val-grid">
        <div v-for="v in valuations.items" :key="v.name" class="val-card">
          <div class="val-head">
            <span class="val-name">{{ v.name }}</span>
            <span class="val-date">截至 {{ v.date }}</span>
          </div>
          <div class="val-pe">PE-TTM <b>{{ v.pe }}</b></div>
          <div class="val-bar">
            <div class="val-bar-fill" :style="{ width: v.pe_percentile + '%' }"></div>
            <span class="val-mark" :style="{ left: v.pe_percentile + '%' }"></span>
          </div>
          <div class="val-pct">
            历史分位 <b :class="v.pe_percentile <= 30 ? 'v-low' : (v.pe_percentile >= 70 ? 'v-high' : '')">{{ v.pe_percentile }}%</b>
          </div>
          <div class="val-span">区间 {{ v.start_date }} ~ {{ v.date }} · {{ v.history_count }} 日</div>
        </div>
      </div>
    </section>

    <!-- 资金与情绪摘要 -->
    <section class="section">
      <div class="section-head">
        <h2>资金与情绪</h2>
        <RouterLink class="link-more" to="/market/sectors">板块轮动 →</RouterLink>
      </div>

      <div class="summary-row">
        <div class="summary-card">
          <div class="slabel">北向资金净买额（合计）</div>
          <div class="svalue" :class="pctClass(northNet)">
            {{ northNet != null ? formatYi(northNet) + ' 亿' : '-' }}
          </div>
          <div class="shint">沪股通 + 深股通</div>
        </div>
        <div class="summary-card">
          <div class="slabel">上涨 / 下跌 / 平盘</div>
          <div class="svalue trio">
            <span class="up">{{ activity.up ?? '-' }}</span>
            <span class="sep">/</span>
            <span class="down">{{ activity.down ?? '-' }}</span>
            <span class="sep">/</span>
            <span>{{ activity.flat ?? '-' }}</span>
          </div>
          <div class="shint">
            涨停 {{ activity.limit_up ?? '-' }} · 跌停 {{ activity.limit_down ?? '-' }}
            <span v-if="activity.activity"> · 活跃度 {{ activity.activity }}</span>
          </div>
        </div>
        <div class="summary-card">
          <div class="slabel">两融余额（沪深）</div>
          <div class="svalue">
            {{ margin.available && margin.total != null ? margin.total.toLocaleString() + ' 亿' : '-' }}
          </div>
          <div class="shint">
            <span v-if="margin.chg_1d != null" :class="pctClass(margin.chg_1d)">
              日变化 {{ margin.chg_1d > 0 ? '+' : '' }}{{ margin.chg_1d }} 亿
            </span>
            <span v-else>日变化 -</span>
            <template v-if="margin.date"> · 截至 {{ margin.date }}</template>
          </div>
          <div v-if="margin.message" class="shint">{{ margin.message }}</div>
        </div>
        <div class="summary-card chart-sm" v-if="activityChartOption.series">
          <v-chart :option="activityChartOption" autoresize style="height: 140px; width: 100%" />
        </div>
      </div>

      <div class="table-wrap" v-if="zt.available">
        <h3>
          涨停池情绪
          <span v-if="zt.date" class="zt-date">
            （{{ zt.date }}{{ zt.is_live ? ' · 盘中实时' : ' · 收盘口径' }}）
          </span>
        </h3>
        <div class="zt-metrics">
          <span>涨停 <b class="up">{{ zt.zt_count ?? '-' }}</b></span>
          <span>炸板 <b>{{ zt.zb_count ?? '-' }}</b></span>
          <span>跌停 <b class="down">{{ zt.dt_count ?? '-' }}</b></span>
          <span>封板率 <b>{{ zt.seal_rate != null ? zt.seal_rate + '%' : '-' }}</b></span>
          <span>最高连板 <b>{{ zt.max_lb != null ? zt.max_lb + ' 板' : '-' }}</b></span>
          <span>连板家数 <b>{{ zt.lb_count ?? '-' }}</b></span>
        </div>
        <table class="data-table" v-if="zt.top?.length">
          <thead>
            <tr>
              <th>代码</th><th>名称</th><th>连板数</th><th>行业</th><th>最新价</th><th>涨跌幅</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in zt.top" :key="row.code">
              <td>{{ row.code }}</td>
              <td>{{ row.name }}</td>
              <td>{{ row.lb }} 板</td>
              <td>{{ row.industry }}</td>
              <td>{{ row.price ?? '-' }}</td>
              <td :class="pctClass(row.pct)">{{ formatPct(row.pct) }}</td>
            </tr>
          </tbody>
        </table>
        <div v-if="zt.message" class="shint">{{ zt.message }}</div>
      </div>

      <div class="table-wrap">
        <h3>沪深港通</h3>
        <table class="data-table" v-if="hsgt.length">
          <thead>
            <tr>
              <th>日期</th>
              <th>板块</th>
              <th>方向</th>
              <th>成交净买额</th>
              <th>上涨/下跌</th>
              <th>相关指数</th>
              <th>指数涨跌</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, i) in hsgt" :key="i">
              <td>{{ row.trade_date }}</td>
              <td>{{ row.board }}</td>
              <td>{{ row.direction }}</td>
              <td :class="pctClass(row.net_buy)">{{ formatYi(row.net_buy) }}</td>
              <td>
                <span class="up">{{ row.up_count ?? '-' }}</span>
                /
                <span class="down">{{ row.down_count ?? '-' }}</span>
              </td>
              <td>{{ row.related_index }}</td>
              <td :class="pctClass(row.index_change_pct)">{{ formatPct(row.index_change_pct) }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty-inline">暂无沪深港通数据</div>
      </div>

      <div class="chart-block">
        <div class="section-head">
          <h3>大盘主力资金净流入<span v-if="flowData?.window?.label" class="flow-label">（{{ flowData.window.label }}）</span></h3>
          <PeriodPicker
            :model-value="flowPeriod"
            :options="FLOW_PERIODS"
            :loading="flowLoading"
            @update:model-value="setFlowPeriod"
          />
        </div>
        <div v-if="flowError" class="error-box sm">{{ flowError }}</div>
        <div class="flow-summary" v-if="flowData?.available && flowSummary.days != null">
          <span>区间交易日 <b>{{ flowSummary.days }}</b></span>
          <span>主力合计 <b :class="pctClass(flowSummary.total_main_net)">{{ formatAmount(flowSummary.total_main_net, { signed: true }) }}</b></span>
          <span>净流入 <b class="up">{{ flowSummary.inflow_days ?? '-' }} 天</b></span>
          <span>净流出 <b class="down">{{ flowSummary.outflow_days ?? '-' }} 天</b></span>
        </div>
        <p v-if="flowNote" class="flow-note">{{ flowNote }}</p>
        <v-chart
          v-if="mainHist.available && mainHist.items?.length"
          :option="fundChartOption"
          autoresize
          style="height: 280px; width: 100%"
        />
        <div v-else class="empty-inline">
          {{ mainHist.message || '主力资金历史暂不可用' }}
        </div>
      </div>

      <div class="concept-block" v-if="concept.available">
        <h3>概念资金 TOP（摘要）</h3>
        <div class="concept-grid">
          <div class="concept-col">
            <div class="concept-title up">净流入</div>
            <table class="data-table">
              <thead><tr><th>板块</th><th>净额</th><th>涨跌幅</th></tr></thead>
              <tbody>
                <tr v-for="(row, i) in (concept.inflow_top || []).slice(0, 5)" :key="'in'+i">
                  <td>{{ row.name }}</td>
                  <td :class="row.net > 0 ? 'up' : row.net < 0 ? 'down' : ''">{{ formatAmount(row.net) }}</td>
                  <td :class="pctClass(row.change_pct)">{{ formatPct(row.change_pct) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="concept-col">
            <div class="concept-title down">净流出</div>
            <table class="data-table">
              <thead><tr><th>板块</th><th>净额</th><th>涨跌幅</th></tr></thead>
              <tbody>
                <tr v-for="(row, i) in (concept.outflow_top || []).slice(0, 5)" :key="'out'+i">
                  <td>{{ row.name }}</td>
                  <td :class="row.net > 0 ? 'up' : row.net < 0 ? 'down' : ''">{{ formatAmount(row.net) }}</td>
                  <td :class="pctClass(row.change_pct)">{{ formatPct(row.change_pct) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.market-page { color: #ddd; }

/* 指数估值分位 */
.val-note { color: #68758d; font-size: 11px; }
.val-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.val-card { background: #111d34; border: 1px solid #213251; border-radius: 10px; padding: 12px; }
.val-head { display: flex; justify-content: space-between; align-items: baseline; }
.val-name { font-size: 14px; color: #e0e7f1; }
.val-date { color: #68758d; font-size: 10px; }
.val-pe { margin-top: 8px; color: #8490a8; font-size: 12px; }
.val-pe b { font-size: 20px; color: #e0e7f1; margin-left: 6px; }
.val-bar { position: relative; height: 8px; background: #0c172a; border-radius: 4px; margin: 10px 0 8px; overflow: visible; }
.val-bar-fill { height: 100%; border-radius: 4px; background: linear-gradient(90deg, #2f6f5e, #d6a13c, #c0392b); }
.val-mark { position: absolute; top: -3px; width: 2px; height: 14px; background: #fff; }
.val-pct { color: #8490a8; font-size: 12px; }
.val-pct b { margin-left: 4px; }
.val-pct b.v-low { color: #d6a13c; }
.val-pct b.v-high { color: #ff8796; }
.val-span { margin-top: 6px; color: #68758d; font-size: 10px; }
@media (max-width: 1000px) { .val-grid { grid-template-columns: repeat(2, 1fr); } }


.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 20px;
  gap: 12px;
}

.page-header h1 {
  font-size: 24px;
  color: #eee;
  margin: 0;
}

.sub {
  color: #888;
  font-size: 13px;
  margin-top: 4px;
}

.btn {
  padding: 8px 18px;
  border: none;
  border-radius: 6px;
  background: #e94560;
  color: #fff;
  cursor: pointer;
  font-size: 14px;
}

.btn:disabled { opacity: 0.6; cursor: not-allowed; }

.error-box {
  background: #3a1520;
  color: #ff8a9a;
  padding: 12px 16px;
  border-radius: 8px;
  margin-bottom: 16px;
}

.section {
  background: #16213e;
  border-radius: 8px;
  padding: 16px 18px;
  margin-bottom: 20px;
}

.section-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
  gap: 12px;
}

.section h2 {
  font-size: 16px;
  color: #eee;
  margin: 0 0 14px;
}

.section-head h2 { margin: 0; }

.section h3 {
  font-size: 14px;
  color: #ccc;
  margin: 16px 0 10px;
}

.link-more {
  color: #7eb8ff;
  font-size: 13px;
  text-decoration: none;
  white-space: nowrap;
}

.link-more:hover { text-decoration: underline; }

.module-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 12px;
}

.module-card {
  display: block;
  background: linear-gradient(145deg, #1a1a2e 0%, #0f3460 120%);
  border: 1px solid #1a2744;
  border-radius: 10px;
  padding: 16px;
  text-decoration: none;
  color: inherit;
  transition: border-color 0.2s, transform 0.15s;
}

.module-card:hover {
  border-color: #e94560;
  transform: translateY(-2px);
}

.mod-title {
  font-size: 16px;
  font-weight: 700;
  color: #eee;
  margin-bottom: 6px;
}

.mod-desc {
  font-size: 12px;
  color: #999;
  line-height: 1.4;
  min-height: 34px;
}

.mod-go {
  margin-top: 10px;
  font-size: 13px;
  color: #e94560;
  font-weight: 600;
}

.index-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 12px;
}

.index-card {
  background: #1a1a2e;
  border-radius: 8px;
  padding: 12px 14px;
  border: 1px solid #1a2744;
}

.idx-name { color: #eee; font-weight: 600; font-size: 14px; }
.idx-code { color: #666; font-size: 12px; margin-bottom: 6px; }
.idx-price { font-size: 22px; font-weight: bold; margin: 4px 0; }
.idx-change {
  display: flex;
  gap: 10px;
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
}
.idx-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 12px;
  color: #aaa;
}
.idx-meta.muted { margin-top: 4px; color: #777; }

.summary-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
  margin-bottom: 8px;
}

.summary-card {
  background: #1a1a2e;
  border-radius: 8px;
  padding: 14px;
}

.summary-card.chart-sm { min-height: 140px; }
.slabel { color: #888; font-size: 12px; margin-bottom: 6px; }
.svalue { font-size: 22px; font-weight: bold; color: #eee; }
.svalue.trio {
  font-size: 18px;
  display: flex;
  align-items: baseline;
  gap: 6px;
}
.sep { color: #555; }
.shint { margin-top: 6px; font-size: 12px; color: #888; }

.zt-date { color: #777; font-size: 12px; font-weight: normal; }
.zt-metrics {
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
  margin: 8px 0 10px;
  font-size: 13px;
  color: #999;
}
.zt-metrics b { color: #eee; margin-left: 4px; }

.table-wrap { overflow-x: auto; }

.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.data-table th {
  background: #0f3460;
  color: #aaa;
  font-weight: normal;
  text-align: left;
  padding: 8px 10px;
  white-space: nowrap;
}

.data-table td {
  padding: 8px 10px;
  border-bottom: 1px solid #1a2744;
  white-space: nowrap;
}

.chart-block { margin-top: 8px; }
.chart-block .section-head { margin: 0 0 10px; }
.chart-block .section-head h3 { margin: 0; }
.flow-label { color: #888; font-weight: normal; font-size: 12px; }
.flow-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  align-items: baseline;
  color: #8896a8;
  font-size: 12px;
  margin-bottom: 8px;
}
.flow-summary b { color: #eee; font-size: 13px; }
.flow-note { color: #b89a5a; font-size: 12px; margin: 0 0 8px; }
.concept-block { margin-top: 12px; }
.concept-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.concept-title { font-size: 13px; font-weight: 600; margin-bottom: 8px; }

.empty, .empty-inline {
  color: #666;
  padding: 24px;
  text-align: center;
  font-size: 13px;
}

.up { color: #e94560; font-weight: 600; }
.down { color: #00c853; font-weight: 600; }

@media (max-width: 640px) {
  .page-header h1 { font-size: 20px; }
  .idx-price { font-size: 18px; }
  .concept-grid { grid-template-columns: 1fr; }
}
</style>
