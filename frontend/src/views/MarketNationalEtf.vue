<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import MarketSubNav from '../components/MarketSubNav.vue'
import MarketDataStatus from '../components/MarketDataStatus.vue'
import EtfDetailPanel from '../components/EtfDetailPanel.vue'
import PeriodPicker from '../components/PeriodPicker.vue'
import { marketApi } from '../api/stocks.js'
import { formatPct, formatNum, formatRate, pctClass } from '../utils/format.js'
import { formatAmount, formatShare } from '../utils/marketFormat.js'
import { errorMessage, isCanceled } from '../utils/errorMessage.js'
import { createRequestGuard } from '../utils/requestGuard.js'
import { escapeHtml } from '../utils/escapeHtml.js'
import { onActivateKey } from '../utils/a11y.js'
import { trendColor } from '../constants/colors.js'
import { parseNationalEtfQuery } from '../utils/routeQuery.js'
import { useRouteQuery } from '../composables/useRouteQuery.js'

// 显式组件名：App.vue 的 KeepAlive include 按它匹配，不依赖文件名推断（FEF-15）
defineOptions({ name: 'MarketNationalEtf' })

use([BarChart, GridComponent, TooltipComponent, CanvasRenderer])

const route = useRoute(), router = useRouter()
const loading = ref(false), error = ref(''), data = ref(null)
const selectedCode = ref(parseNationalEtfQuery(route.query).selectedCode)
// 前进/后退切换选中的 ETF（FEF-15）
useRouteQuery('MarketNationalEtf', { selectedCode }, parseNationalEtfQuery, () => {})
const items = computed(() => data.value?.items || [])
const summary = computed(() => data.value?.summary || {})
const signals = computed(() => data.value?.signals || null)

const verdictText = computed(() => {
  const s = signals.value
  if (!s?.available) return ''
  if (s.signal === 'sync_in') return '同步净申购信号'
  if (s.signal === 'sync_out') return '同步净赎回信号'
  if (s.signal === 'mixed') return '申赎两向均显著'
  return '无显著异动'
})


async function load() {
  loading.value = true; error.value = ''
  try { data.value = (await marketApi.getNationalEtf()).data }
  catch (e) { error.value = errorMessage(e, '加载观察名单失败') }
  finally { loading.value = false }
}
function select(row) { selectedCode.value = row.code; router.replace({ query: { code: row.code } }) }

// ── 区间资金流向（历史每日主力净流入聚合） ──────────────
const FLOW_PERIODS = [
  ['1d', '当日'], ['3d', '3日'], ['5d', '5日'], ['1w', '近1周'],
  ['1m', '近1月'], ['3m', '近3月'], ['6m', '近半年'], ['ytd', '今年以来'], ['custom', '自定义'],
]
const flowPeriod = ref('3m')
const flowCustomStart = ref('')
const flowCustomEnd = ref('')
const flowLoading = ref(false), flowError = ref(''), flowData = ref(null)
const flowItems = computed(() => flowData.value?.items || [])
const flowSummary = computed(() => flowData.value?.summary || {})

// 区间请求较慢（冷启动可达数十秒），切换区间时取消旧请求、丢弃旧响应（FEF-06）
const flowGuard = createRequestGuard()

async function loadFlow() {
  const req = flowGuard.next()
  flowLoading.value = true; flowError.value = ''
  try {
    const params = flowPeriod.value === 'custom'
      ? { start: flowCustomStart.value, end: flowCustomEnd.value }
      : { period: flowPeriod.value }
    const res = await marketApi.getNationalEtfFlow(params, { signal: req.signal })
    if (req.isLatest()) flowData.value = res.data
  } catch (e) {
    if (req.isLatest() && !isCanceled(e)) flowError.value = errorMessage(e, '加载区间资金流向失败')
  } finally { if (req.isLatest()) flowLoading.value = false }
}
function setFlowPeriod(p) {
  if (flowPeriod.value === p) return
  flowPeriod.value = p
  if (p !== 'custom') loadFlow()
}
function applyCustomFlow() {
  if (!flowCustomStart.value && !flowCustomEnd.value) return
  loadFlow()
}

const flowBarOption = computed(() => {
  const rows = flowItems.value
    .filter(r => r.available)
    .slice()
    .sort((a, b) => (a.total_main_net || 0) - (b.total_main_net || 0))
  if (!rows.length) return {}
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      backgroundColor: '#101827', borderColor: '#33415f', textStyle: { color: '#ddd' },
      formatter: (p) => {
        const row = rows[p.dataIndex]
        // ETF 名称来自第三方数据源，拼 HTML 前转义（SEC-09）
        return `${escapeHtml(row.name)}（${escapeHtml(row.code)}）<br/>区间主力净流入 ${formatAmount(row.total_main_net, { signed: true })}` +
          `<br/>净流入 ${row.up_days} 天 / 净流出 ${row.down_days} 天` +
          `<br/>最新收盘 ${formatNum(row.last_close)}（区间 ${formatPct(row.window_change_pct)}）`
      },
    },
    grid: { left: 8, right: 78, top: 6, bottom: 6, containLabel: true },
    xAxis: { type: 'value', axisLabel: { show: false }, splitLine: { show: false } },
    yAxis: {
      type: 'category', data: rows.map(r => r.name),
      axisLabel: { color: '#a6b1c5', fontSize: 11 }, axisLine: { show: false }, axisTick: { show: false },
    },
    series: [{
      type: 'bar',
      barWidth: 10,
      data: rows.map(r => ({
        value: Math.round(r.total_main_net || 0),
        itemStyle: { color: trendColor(r.total_main_net), borderRadius: 3 },
      })),
      label: {
        show: true, position: 'right', fontSize: 10,
        color: '#9eabc1', formatter: p => formatAmount(p.value, { signed: true }),
      },
    }],
  }
})

onMounted(() => { load(); loadFlow() })
onUnmounted(() => flowGuard.abort())
</script>

<template>
  <div class="page">
    <MarketSubNav />
    <div class="page-header">
      <div><div class="title-line"><h1>国家队相关 ETF 观察</h1><span>观察名单 · 非官方持仓</span>
        <span v-if="data?.meta?.source_data_date" class="date-chip">数据截至 {{ data.meta.source_data_date }}</span></div>
        <p>宽基与政策相关 ETF 的行情、份额与资金快照</p></div>
      <button class="primary" @click="load" :disabled="loading">{{ loading ? '加载中...' : '刷新' }}</button>
    </div>
    <div v-if="error" class="error-box">{{ error }}</div>
    <div class="warning"><b>数据边界</b> {{ data?.disclaimer || '该页面是研究观察名单，不代表任何机构的官方持仓。' }}</div>
    <div v-if="data?.watchlist_definition" class="definition">
      <span><b>名单维护：</b>{{ data.watchlist_definition.maintained_by }}</span>
      <span><b>筛选依据：</b>{{ data.watchlist_definition.basis }}</span>
      <span><b>官方持仓：</b>{{ data.watchlist_definition.is_official_holding ? '是' : '否' }}</span>
    </div>
    <div class="summary-grid">
      <div><label>有行情标的</label><b>{{ summary.count ?? '-' }} 只</b></div>
      <div><label>合计主力净流入</label><b :class="pctClass(summary.total_main_net)">{{ formatAmount(summary.total_main_net, { signed: true }) }}</b></div>
      <div><label>合计最新份额</label><b>{{ formatShare(summary.total_share) }}</b></div>
      <div><label>合计市场规模</label><b>{{ formatAmount(summary.total_market_cap) }}</b></div>
    </div>
    <section v-if="signals" class="signal-card">
      <div class="signal-head">
        <h2>份额异动信号（宽基）</h2>
        <small v-if="signals.available">{{ signals.prev_date }} → {{ signals.date }} 收盘对比</small>
      </div>
      <template v-if="signals.available">
        <div class="signal-verdict">
          <b :class="signals.signal">{{ verdictText }}</b>
          <span>
            净申购 {{ signals.in_count ?? 0 }} 只<template v-if="signals.in_total_chg != null">（合计 {{ formatShare(signals.in_total_chg, { signed: true }) }}）</template>
            · 净赎回 {{ signals.out_count ?? 0 }} 只<template v-if="signals.out_total_chg != null">（合计 {{ formatShare(signals.out_total_chg, { signed: true }) }}）</template>
          </span>
        </div>
        <div v-if="signals.in_items?.length || signals.out_items?.length" class="signal-lists">
          <div v-if="signals.in_items?.length" class="signal-col">
            <label>份额显著增加</label>
            <div v-for="row in signals.in_items" :key="row.code" class="signal-row">
              <span class="code">{{ row.code }}</span><span class="name">{{ row.name }}</span>
              <b :class="pctClass(row.share_chg)">{{ formatShare(row.share_chg, { signed: true }) }}</b>
              <b :class="pctClass(row.share_chg_pct)">{{ formatPct(row.share_chg_pct) }}</b>
              <b :class="pctClass(row.est_amount)">{{ formatAmount(row.est_amount, { signed: true }) }}</b>
            </div>
          </div>
          <div v-if="signals.out_items?.length" class="signal-col">
            <label>份额显著减少</label>
            <div v-for="row in signals.out_items" :key="row.code" class="signal-row">
              <span class="code">{{ row.code }}</span><span class="name">{{ row.name }}</span>
              <b :class="pctClass(row.share_chg)">{{ formatShare(row.share_chg, { signed: true }) }}</b>
              <b :class="pctClass(row.share_chg_pct)">{{ formatPct(row.share_chg_pct) }}</b>
              <b :class="pctClass(row.est_amount)">{{ formatAmount(row.est_amount, { signed: true }) }}</b>
            </div>
          </div>
        </div>
        <p v-else class="signal-note">今日无达到阈值的宽基份额异动。</p>
        <p class="signal-note">{{ signals.message }}</p>
      </template>
      <p v-else class="signal-note">{{ signals.message }}</p>
    </section>
    <div class="workbench">
      <section class="table-card"><div class="table-wrap"><table>
        <thead><tr><th>#</th><th>代码</th><th>ETF 名称</th><th>最新价</th><th>涨跌幅</th><th>主力净流入</th><th>最新份额</th><th>总市值</th><th>换手</th><th>折溢价</th></tr></thead>
        <tbody><tr v-for="(row,i) in items" :key="row.code" :class="{ selected:selectedCode===row.code, muted:!row.listed }" :tabindex="row.listed ? 0 : -1" @click="row.listed && select(row)" @keydown="row.listed && onActivateKey($event, () => select(row))">
          <td>{{ i+1 }}</td><td class="code">{{ row.code }}</td><td class="name">{{ row.name }} <small v-if="!row.listed">暂无行情</small></td><td>{{ formatNum(row.price) }}</td>
          <td :class="pctClass(row.change_pct)">{{ formatPct(row.change_pct) }}</td><td :class="pctClass(row.main_net)">{{ formatAmount(row.main_net, { signed: true }) }}</td>
          <td>{{ formatShare(row.share) }}</td><td>{{ formatAmount(row.market_cap) }}</td><td>{{ formatRate(row.turnover_rate) }}</td><td :class="pctClass(row.discount_rate)">{{ formatPct(row.discount_rate) }}</td>
        </tr></tbody>
      </table><div v-if="!items.length&&!loading" class="empty">暂无观察数据</div></div></section>
      <EtfDetailPanel :code="selectedCode" />
    </div>

    <section class="flow-panel">
      <div class="flow-head">
        <div>
          <h2>区间资金流向（主力净流入合计）</h2>
          <small>上游仅提供最近约 120 个交易日历史；为当日资金快照口径，不代表真实持仓变化</small>
        </div>
      <div class="flow-tabs-wrap">
        <PeriodPicker
          :model-value="flowPeriod"
          :options="FLOW_PERIODS"
          :loading="flowLoading"
          @update:model-value="setFlowPeriod"
        />
        <div v-if="flowPeriod === 'custom'" class="custom-range">
          <input v-model="flowCustomStart" type="date" aria-label="开始日期" />
          <span>至</span>
          <input v-model="flowCustomEnd" type="date" aria-label="结束日期" />
          <button type="button" class="custom-apply" :disabled="flowLoading" @click="applyCustomFlow">查询</button>
        </div>
      </div>
    </div>
    <div v-if="flowError" class="error-box">{{ flowError }}</div>
    <div v-if="flowData?.note" class="flow-note">{{ flowData.note }}</div>
      <div v-if="flowData?.summary" class="flow-summary">
        <div><label>区间合计净流入</label><b :class="pctClass(flowSummary.total_main_net)">{{ formatAmount(flowSummary.total_main_net, { signed: true }) }}</b></div>
        <div><label>净流入 / 流出家数</label><b>{{ flowSummary.inflow_count ?? '-' }} / {{ flowSummary.outflow_count ?? '-' }}</b></div>
        <div><label>覆盖区间</label><b>{{ flowData.coverage_start || '-' }} ~ {{ flowData.end || '-' }}</b></div>
        <div><label>数据截至交易日</label><b>{{ flowData.meta?.data_as_of || flowData.end || '-' }}</b></div>
      </div>
      <div v-if="flowData?.failed_codes?.length" class="flow-note">
        拉取失败：{{ flowData.failed_codes.join('、') }}，合计值不含这些标的，可稍后点时间段重新加载
      </div>
      <div v-if="flowLoading" class="flow-loading">正在逐只拉取 18 只 ETF 的历史资金流，首次约 30–90 秒...</div>
      <v-chart v-else-if="flowBarOption.series" :option="flowBarOption" autoresize style="height: 540px; width: 100%" />
      <div v-else-if="!flowError" class="flow-loading">暂无区间资金流数据</div>
    </section>

    <MarketDataStatus :meta="data?.meta" :fallback="data?.disclaimer" />
  </div>
</template>

<style scoped>
.page{color:#dce4f2}.page-header{display:flex;justify-content:space-between;gap:12px;margin-bottom:11px}.page-header h1{margin:0;font-size:22px}.page-header p{margin:4px 0 0;color:#8391a7;font-size:13px}.title-line{display:flex;align-items:center;gap:9px;flex-wrap:wrap}.title-line span{color:#ffbd6b;background:#382711;border:1px solid #694718;border-radius:5px;padding:3px 7px;font-size:11px}.title-line span.date-chip{color:#8fc6d6;background:#122736;border-color:#2e4a5e}.primary{cursor:pointer;border:1px solid #286391;background:#174673;color:#fff;border-radius:6px;padding:7px 13px}.primary:disabled{opacity:.5}.error-box{background:var(--danger-bg);color:var(--danger);padding:10px;border-radius:7px;margin-bottom:10px}.warning{background:#211b15;border:1px solid #584126;color:#cbb892;padding:10px 12px;border-radius:8px;font-size:12px;margin-bottom:10px;line-height:1.5}.warning b{color:#f0bd71;margin-right:6px}.definition{display:flex;flex-wrap:wrap;gap:8px 18px;background:#0d1729;border:1px solid #293b5b;border-radius:8px;padding:9px 11px;margin-bottom:10px;color:#8391a8;font-size:12px}.definition b{color:#aeb9cb}.summary-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:9px;margin-bottom:10px}.summary-grid>div{background:#111d34;border:1px solid #213251;border-radius:8px;padding:11px}.summary-grid label{display:block;color:#8691a4;font-size:11px}.summary-grid b{display:block;margin-top:5px;font-size:17px}
.signal-card{background:#111d34;border:1px solid #213251;border-radius:10px;padding:12px 14px;margin-bottom:10px}.signal-head{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:9px}.signal-head h2{margin:0;font-size:15px;color:#e6ecf4}.signal-head small{color:#8691a4;font-size:11px}.signal-verdict{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:baseline;background:#0d1729;border:1px solid #213251;border-radius:8px;padding:10px 12px;margin-bottom:9px}.signal-verdict b{font-size:15px;color:#8fa0b8}.signal-verdict b.sync_in{color:var(--up)}.signal-verdict b.sync_out{color:var(--down)}.signal-verdict b.mixed{color:#ffbd6b}.signal-verdict span{color:#9eabc1;font-size:12px}.signal-lists{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;margin-bottom:9px}.signal-col{background:#0d1729;border:1px solid #213251;border-radius:8px;padding:9px 11px}.signal-col label{display:block;color:#8691a4;font-size:11px;margin-bottom:6px}.signal-row{display:grid;grid-template-columns:64px minmax(0,1fr) 92px 68px 84px;gap:6px;align-items:baseline;padding:4px 0;border-top:1px solid #1d2a43;font-size:12px}.signal-col .signal-row:first-of-type{border-top:0}.signal-row .code{color:#7eb8e8}.signal-row .name{color:#dce4f2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.signal-row b{text-align:right}.signal-note{margin:0;color:#988e86;font-size:11px;line-height:1.5}.workbench{display:grid;grid-template-columns:minmax(0,1.8fr) minmax(320px,.9fr);gap:10px;margin-bottom:10px;align-items:start}.table-card{min-width:0;background:#111d34;border:1px solid #213251;border-radius:10px;overflow:hidden}.table-wrap{overflow:auto;max-height:720px}table{width:100%;border-collapse:collapse;font-size:12px}th{position:sticky;top:0;background:#0d192d;color:#8391a9;font-weight:500;text-align:right;padding:9px 8px;white-space:nowrap}td{padding:8px;border-bottom:1px solid #1d2a43;text-align:right;white-space:nowrap}th:nth-child(-n+3),td:nth-child(-n+3){text-align:left}tbody tr{cursor:pointer}tbody tr:hover,tbody tr.selected{background:#162b49}tr.muted{opacity:.5;cursor:not-allowed}.code{color:#7eb8e8}.name{color:#e0e7f1}.name small{color:#988e86;font-size:10px;margin-left:4px}.empty{text-align:center;padding:40px;color:#8690a4}
.flow-panel{background:#111d34;border:1px solid #213251;border-radius:10px;padding:12px 14px;margin-bottom:10px}.flow-head{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-start;justify-content:space-between;margin-bottom:10px}.flow-head h2{margin:0;font-size:15px;color:#e6ecf4}.flow-head small{display:block;margin-top:4px;color:#8691a4;font-size:11px}.flow-tabs-wrap{display:flex;flex-direction:column;gap:8px;align-items:flex-end}.custom-range{display:flex;gap:6px;align-items:center;font-size:12px;color:#9eabc1}.custom-range input{background:#0d1729;border:1px solid #293b5b;color:#ddd;border-radius:5px;padding:4px 7px;color-scheme:dark}.custom-apply{cursor:pointer;border:1px solid #286391;background:#174673;color:#fff;border-radius:5px;padding:5px 12px}.custom-apply:disabled{opacity:.5}.flow-summary{display:grid;grid-template-columns:repeat(4,1fr);gap:9px;margin-bottom:10px}.flow-summary>div{background:#0d1729;border:1px solid #213251;border-radius:8px;padding:10px}.flow-summary label{display:block;color:#8691a4;font-size:11px}.flow-summary b{display:block;margin-top:4px;font-size:16px}.flow-note{border:1px solid #554526;background:#201b14;color:#e8b766;border-radius:7px;padding:8px 10px;margin-bottom:10px;font-size:12px}.flow-loading{text-align:center;padding:60px 20px;color:#8690a4;font-size:13px}
@media(max-width:1000px){.workbench{grid-template-columns:1fr}.summary-grid,.flow-summary{grid-template-columns:repeat(2,1fr)}}
</style>
