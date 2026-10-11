<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import MarketSubNav from '../components/MarketSubNav.vue'
import MarketDataStatus from '../components/MarketDataStatus.vue'
import EtfDetailPanel from '../components/EtfDetailPanel.vue'
import { marketApi } from '../api/stocks.js'
import { formatNum, formatPct, formatRate, pctClass } from '../utils/format.js'
import { formatAmount, formatShare } from '../utils/marketFormat.js'
import { errorMessage, isCanceled } from '../utils/errorMessage.js'
import { createRequestGuard } from '../utils/requestGuard.js'
import { parseEtfRadarQuery } from '../utils/routeQuery.js'
import { useRouteQuery } from '../composables/useRouteQuery.js'

// 显式组件名：App.vue 的 KeepAlive include 按它匹配，不依赖文件名推断（FEF-15）
defineOptions({ name: 'MarketEtfRadar' })

const route = useRoute()
const router = useRouter()
const loading = ref(false)
const error = ref('')
const data = ref(null)
const initial = parseEtfRadarQuery(route.query)
const scope = ref(initial.scope)
const rank = ref(initial.rank)
const sort = ref(initial.sort)
const order = ref(initial.order)
const q = ref(initial.q)
const minTurnoverYi = ref(initial.minTurnoverYi)
const page = ref(initial.page)
const selectedCode = ref(initial.selectedCode)

// 缓存页的 query 被外部改变时重新应用；只换了选中 ETF 时不必重拉榜单（FEF-15）
useRouteQuery(
  'MarketEtfRadar',
  { scope, rank, sort, order, q, minTurnoverYi, page, selectedCode },
  parseEtfRadarQuery,
  (keys) => { if (keys.some(k => k !== 'selectedCode')) load() },
)

const ranks = [
  ['share', '份额规模'], ['market_cap', '市值'], ['turnover', '成交额'],
  ['main_inflow', '主力流入'], ['main_outflow', '主力流出'], ['gainer', '涨幅'], ['loser', '跌幅'],
]
const items = computed(() => data.value?.items || [])
const summary = computed(() => data.value?.summary || {})
const pagination = computed(() => data.value?.pagination || { page: 1, total_pages: 1, total: 0 })
const supported = computed(() => data.value?.supported_metrics || {})

function syncUrl() {
  // 晚到的调用不能改写别的页面的地址
  if (route.name !== 'MarketEtfRadar') return
  router.replace({ query: {
    ...(scope.value !== 'equity_broad' ? { scope: scope.value } : {}),
    ...(rank.value !== 'share' ? { rank: rank.value } : {}),
    ...(sort.value ? { sort: sort.value, order: order.value } : {}),
    ...(q.value ? { q: q.value } : {}),
    ...(minTurnoverYi.value ? { min_turnover_yi: minTurnoverYi.value } : {}),
    ...(page.value > 1 ? { page: page.value } : {}),
    ...(selectedCode.value ? { code: selectedCode.value } : {}),
  } })
}

// 切换榜单/排序/翻页时取消旧请求、丢弃晚到的旧响应（FEF-06）
const guard = createRequestGuard()

async function load(resetPage = false) {
  if (resetPage) page.value = 1
  const req = guard.next()
  loading.value = true
  error.value = ''
  syncUrl()
  try {
    const min = Number(minTurnoverYi.value)
    const res = await marketApi.getEtfRadar({
      scope: scope.value, rank: rank.value,
      sort: sort.value || undefined, order: sort.value ? order.value : undefined,
      q: q.value || undefined,
      min_turnover: minTurnoverYi.value && Number.isFinite(min) ? min * 1e8 : undefined,
      page: page.value, page_size: 50,
    }, { signal: req.signal })
    if (!req.isLatest()) return
    data.value = res.data
    page.value = res.data?.pagination?.page || 1
  } catch (e) {
    if (!req.isLatest() || isCanceled(e)) return
    error.value = errorMessage(e, '加载 ETF 雷达失败')
  } finally { if (req.isLatest()) loading.value = false }
}

function select(row) { selectedCode.value = row.code; syncUrl() }
function changePage(delta) { page.value += delta; load() }
function changeRank(value) { rank.value = value; sort.value = ''; order.value = 'desc'; load(true) }
function changeScope(value) { scope.value = value; load(true) }
function sortBy(field) {
  if (sort.value === field) order.value = order.value === 'desc' ? 'asc' : 'desc'
  else { sort.value = field; order.value = 'desc' }
  load(true)
}
function sortMark(field) {
  if (sort.value !== field) return '↕'
  return order.value === 'desc' ? '↓' : '↑'
}

onMounted(() => load())
onUnmounted(() => guard.abort())
</script>

<template>
  <div class="page">
    <MarketSubNav />
    <div class="page-header">
      <div><div class="title-line"><h1>ETF 份额雷达</h1><span v-if="data?.meta?.source_data_date" class="date-chip">数据截至 {{ data.meta.source_data_date }}</span></div><p>股票/宽基规则筛选与全市场 ETF 快照研究台 · 份额 1/5/20 日变化由本站日度快照计算，窗口齐全后自动显示</p></div>
      <button class="primary" @click="load()" :disabled="loading">{{ loading ? '加载中...' : '刷新' }}</button>
    </div>
    <div v-if="error" class="error-box">{{ error }}</div>

    <div class="scope-row">
      <button :class="{ active: scope === 'equity_broad' }" @click="changeScope('equity_broad')">股票/宽基（规则）</button>
      <button :class="{ active: scope === 'all' }" @click="changeScope('all')">全市场 ETF</button>
      <span v-if="data?.scope">规则 {{ data.scope.rule_version }} · 默认范围 {{ data.scope.equity_broad_count }} / 全市场 {{ data.scope.all_count }}</span>
    </div>
    <div class="scope-note">股票/宽基为研究用名称规则筛选，并非基金管理人的官方分类；切换“全市场 ETF”可查看未纳入默认范围的标的。</div>

    <div class="summary-grid">
      <div><label>当前范围</label><b>{{ summary.count ?? '-' }} 只</b></div>
      <div><label>合计成交额</label><b>{{ formatAmount(summary.total_turnover) }}</b></div>
      <div><label>主力净流入</label><b :class="pctClass(summary.total_main_net)">{{ formatAmount(summary.total_main_net, { signed: true }) }}</b></div>
      <div><label>份额字段覆盖</label><b>{{ summary.share_available_count ?? '-' }} 只</b></div>
    </div>

    <div class="rank-tabs">
      <button v-for="r in ranks" :key="r[0]" :class="{ active: rank === r[0] }" @click="changeRank(r[0])">{{ r[1] }}</button>
    </div>
    <div class="filters">
      <input v-model.trim="q" placeholder="搜索 ETF 名称或代码" @keyup.enter="load(true)" />
      <input v-model="minTurnoverYi" type="number" min="0" placeholder="最低成交额（亿）" @keyup.enter="load(true)" />
      <button @click="load(true)">筛选</button>
      <button class="ghost" @click="q=''; minTurnoverYi=''; load(true)">重置</button>
    </div>

    <div v-if="data?.supported_metrics && !data.supported_metrics.share_change_1d" class="metric-note">
      <b>份额变化暂不可用</b><span>{{ data.supported_metrics.reason }}</span>
    </div>

    <div class="workbench">
      <section class="table-card">
        <div class="table-wrap">
          <table>
            <thead><tr>
              <th>#</th><th>代码</th><th>名称</th><th>最新价</th><th @click="sortBy('change_pct')">涨跌幅 {{ sortMark('change_pct') }}</th>
              <th @click="sortBy('turnover')">成交额 {{ sortMark('turnover') }}</th><th @click="sortBy('main_net')">主力净流入 {{ sortMark('main_net') }}</th>
              <th @click="sortBy('share')">最新份额 {{ sortMark('share') }}</th>
              <th v-if="supported.share_change_1d">份额1日</th>
              <th v-if="supported.share_change_5d">份额5日</th>
              <th v-if="supported.share_change_20d">份额20日</th>
              <th @click="sortBy('market_cap')">总市值 {{ sortMark('market_cap') }}</th><th @click="sortBy('turnover_rate')">换手 {{ sortMark('turnover_rate') }}</th><th @click="sortBy('discount_rate')">折溢价 {{ sortMark('discount_rate') }}</th>
            </tr></thead>
            <tbody>
              <tr v-for="(row, i) in items" :key="row.code" :class="{ selected: selectedCode === row.code }" tabindex="0" @click="select(row)" @keyup.enter="select(row)">
                <td>{{ (pagination.page - 1) * pagination.page_size + i + 1 }}</td><td class="code">{{ row.code }}</td><td class="name">{{ row.name }}</td><td>{{ formatNum(row.price) }}</td>
                <td :class="pctClass(row.change_pct)">{{ formatPct(row.change_pct) }}</td><td>{{ formatAmount(row.turnover) }}</td>
                <td :class="pctClass(row.main_net)">{{ formatAmount(row.main_net, { signed: true }) }}</td><td>{{ formatShare(row.share) }}</td>
                <td v-if="supported.share_change_1d" :class="pctClass(row.share_chg_1d)">{{ formatShare(row.share_chg_1d, { signed: true }) }}</td>
                <td v-if="supported.share_change_5d" :class="pctClass(row.share_chg_5d)">{{ formatShare(row.share_chg_5d, { signed: true }) }}</td>
                <td v-if="supported.share_change_20d" :class="pctClass(row.share_chg_20d)">{{ formatShare(row.share_chg_20d, { signed: true }) }}</td>
                <td>{{ formatAmount(row.market_cap) }}</td><td>{{ formatRate(row.turnover_rate) }}</td><td :class="pctClass(row.discount_rate)">{{ formatPct(row.discount_rate) }}</td>
              </tr>
            </tbody>
          </table>
          <div v-if="!items.length && !loading" class="empty">暂无符合条件的 ETF</div>
        </div>
        <div class="pager">
          <span>共 {{ pagination.total }} 条 · 第 {{ pagination.page }}/{{ pagination.total_pages }} 页</span>
          <div><button :disabled="pagination.page <= 1 || loading" @click="changePage(-1)">上一页</button><button :disabled="pagination.page >= pagination.total_pages || loading" @click="changePage(1)">下一页</button></div>
        </div>
      </section>
      <EtfDetailPanel :code="selectedCode" />
    </div>
    <MarketDataStatus :meta="data?.meta" />
  </div>
</template>

<style scoped>
.page { color: #dce4f2; }.page-header { display:flex; justify-content:space-between; gap:12px; margin-bottom:12px; }.page-header h1 { margin:0; font-size:22px; }.page-header p { margin:4px 0 0; color:#71809a; font-size:13px; }.title-line { display:flex; align-items:center; gap:9px; flex-wrap:wrap; }.date-chip { color:#8fc6d6; background:#122736; border:1px solid #2e4a5e; border-radius:5px; padding:3px 7px; font-size:11px; }
button { cursor:pointer; border:1px solid #293b5b; background:#111d34; color:#9eabc1; border-radius:6px; padding:7px 12px; }.primary,.rank-tabs button.active,.scope-row button.active { background:#174673; color:#fff; border-color:#286391; }button:disabled { opacity:.45; cursor:not-allowed; }.error-box { background:#3a1520; color:#ff8796; padding:10px; border-radius:7px; margin-bottom:10px; }
.scope-row,.rank-tabs,.filters { display:flex; flex-wrap:wrap; gap:7px; align-items:center; margin-bottom:10px; }.scope-row span { color:#65738d; font-size:12px; margin-left:auto; }
.scope-note,.metric-note { border:1px solid #293b5b; background:#0d1729; color:#71809a; border-radius:7px; padding:8px 10px; margin:-3px 0 10px; font-size:12px; line-height:1.5; }.metric-note { display:flex; gap:8px; border-color:#554526; background:#201b14; }.metric-note b { color:#e8b766; }.metric-note span { color:#9e927d; }
.summary-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:9px; margin-bottom:10px; }.summary-grid>div { background:#111d34; border:1px solid #213251; border-radius:8px; padding:11px; }.summary-grid label { color:#68758d; font-size:11px; display:block; }.summary-grid b { display:block; margin-top:5px; font-size:17px; }
.filters input { background:#0d1729; border:1px solid #293b5b; color:#ddd; border-radius:6px; padding:8px 10px; min-width:180px; }.ghost { background:transparent; }
.workbench { display:grid; grid-template-columns:minmax(0, 1.9fr) minmax(320px, .9fr); gap:10px; margin-bottom:10px; align-items:start; }.table-card { min-width:0; background:#111d34; border:1px solid #213251; border-radius:10px; overflow:hidden; }.table-wrap { overflow:auto; max-height:720px; }table { width:100%; border-collapse:collapse; font-size:12px; }th { position:sticky; top:0; z-index:1; background:#0d192d; color:#7887a1; font-weight:500; text-align:right; padding:9px 8px; white-space:nowrap; cursor:pointer; }td { padding:8px; border-bottom:1px solid #1d2a43; text-align:right; white-space:nowrap; }th:nth-child(-n+3),td:nth-child(-n+3) { text-align:left; }tbody tr { cursor:pointer; }tbody tr:hover,tbody tr.selected { background:#162b49; }td.name { color:#e0e7f1; }.code { color:#7eb8e8; }.pager { display:flex; justify-content:space-between; align-items:center; padding:9px; color:#68758d; font-size:12px; }.pager button { margin-left:6px; }.empty { padding:40px; text-align:center; color:#64718a; }
@media(max-width:1000px){.workbench{grid-template-columns:1fr}.summary-grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:600px){.summary-grid{grid-template-columns:1fr 1fr}.page-header{align-items:flex-start}.filters input{width:100%;box-sizing:border-box}.scope-row span{width:100%;margin:0}.table-wrap{max-height:560px}}
</style>
