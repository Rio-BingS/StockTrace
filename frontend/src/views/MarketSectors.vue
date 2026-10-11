<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import MarketSubNav from '../components/MarketSubNav.vue'
import MarketDataStatus from '../components/MarketDataStatus.vue'
import PeriodPicker from '../components/PeriodPicker.vue'
import SectorFlowStage from '../components/SectorFlowStage.vue'
import SectorInsights from '../components/SectorInsights.vue'
import { marketApi } from '../api/stocks.js'
import { formatDateTime, formatPct, pctClass } from '../utils/format.js'
import { formatAmount } from '../utils/marketFormat.js'
import { signedAmount } from '../utils/sectorFlow.js'
import { errorMessage } from '../utils/errorMessage.js'

const route = useRoute()
const router = useRouter()
const loading = ref(false)
const error = ref('')
const data = ref(null)
const board = ref(route.query.board === 'concept' ? 'concept' : 'industry')
const period = ref(['5d', '10d', '20d'].includes(route.query.period) ? route.query.period : 'day')
const q = ref(route.query.q || '')
const sort = ref(route.query.sort || 'net')
const order = ref(route.query.order === 'asc' ? 'asc' : 'desc')
const page = ref(Number(route.query.page) || 1)

const items = computed(() => data.value?.items || [])
const summary = computed(() => data.value?.summary || {})
const pagination = computed(() => data.value?.pagination || {
  page: 1,
  total_pages: 1,
  total: 0,
})

async function load(reset = false) {
  if (reset) page.value = 1
  loading.value = true
  error.value = ''
  syncUrl()
  try {
    data.value = (await marketApi.getSectors({
      board: board.value,
      period: period.value,
      q: q.value || undefined,
      sort: sort.value,
      order: order.value,
      page: page.value,
      page_size: 50,
    })).data
    page.value = data.value.pagination?.page || 1
    syncUrl()
  } catch (e) {
    error.value = errorMessage(e, '加载板块资金失败')
  } finally {
    loading.value = false
  }
}

function syncUrl() {
  router.replace({
    query: {
      ...(board.value === 'concept' ? { board: 'concept' } : {}),
      ...(period.value !== 'day' ? { period: period.value } : {}),
      ...(q.value ? { q: q.value } : {}),
      ...(sort.value !== 'net' ? { sort: sort.value } : {}),
      ...(order.value !== 'desc' ? { order: order.value } : {}),
      ...(page.value > 1 ? { page: page.value } : {}),
    },
  })
}

function switchBoard(value) {
  board.value = value
  load(true)
}

function switchPeriod(value) {
  if (period.value === value) return
  period.value = value
  load(true)
}

function changePage(delta) {
  page.value += delta
  load()
}

function sortBy(field) {
  if (period.value === '20d' && field !== 'net') return
  if (sort.value === field) order.value = order.value === 'desc' ? 'asc' : 'desc'
  else {
    sort.value = field
    order.value = 'desc'
  }
  load(true)
}

function sortMark(field) {
  if (sort.value !== field) return '↕'
  return order.value === 'desc' ? '↓' : '↑'
}

onMounted(load)
</script>

<template>
  <div class='page'>
    <MarketSubNav />
    <header class='page-header'>
      <div>
        <div class='title-row'>
          <h1>板块资金轮动</h1>
          <span class='live-badge'>数据截至 {{ data?.meta?.data_as_of || '最近交易日' }}</span>
        </div>
        <p>行业与概念板块的资金强弱、集中度和价流背离</p>
      </div>
      <div class='header-meta'>
        <span v-if='data?.updated_at'>更新 {{ formatDateTime(data.updated_at) }}</span>
        <button class='icon-button' type='button' title='刷新' aria-label='刷新' :disabled='loading' @click='load()'>↻</button>
      </div>
    </header>
    <div v-if='error' class='error-box'>{{ error }}</div>
    <section class='control-bar' aria-label='板块资金筛选'>
      <div class='segmented' aria-label='板块类型'>
        <button type='button' :class='{ active: board === `industry` }' @click='switchBoard(`industry`)'>行业资金</button>
        <button type='button' :class='{ active: board === `concept` }' @click='switchBoard(`concept`)'>概念资金</button>
      </div>
      <div>
        <PeriodPicker
          :model-value="period"
          :options="[['day', '当日'], ['5d', '5日'], ['10d', '10日'], ['20d', '20日']]"
          :disabled-options="[
            ['1m', '1月', '需要日度快照积累后开放'],
            ['3m', '3月', '需要日度快照积累后开放'],
            ['1y', '1年', '需要日度快照积累后开放'],
          ]"
          :loading="loading"
          @update:model-value="switchPeriod"
        />
        <small class="period-note">
          {{ period === 'day' ? '当日横截面' : period === '20d' ? '20 日累计（本站日度快照）' : '多日累计（快照优先，东财兜底）' }}
          · 1月/3月/1年待日度快照积累后开放
        </small>
      </div>
      <form class='search-form' @submit.prevent='load(true)'>
        <input v-model.trim='q' aria-label='搜索板块或领涨股' placeholder='搜索板块或领涨股' />
        <button type='submit'>查询</button>
      </form>
    </section>

    <div v-if="period === '20d' && data?.message" class="snapshot-note">{{ data.message }}</div>

    <section class='snapshot-strip' aria-label='资金概览'>
      <div><span>板块样本</span><strong>{{ summary.sample_count ?? '-' }}</strong></div>
      <div><span>流入 / 流出</span><strong>{{ summary.inflow_count ?? '-' }} / {{ summary.outflow_count ?? '-' }}</strong></div>
      <div><span>资金广度</span><strong>{{ formatPct(summary.breadth_pct) }}</strong></div>
      <div><span>前三集中度</span><strong>{{ formatPct(summary.top_three_inflow_concentration_pct) }}</strong></div>
    </section>

    <SectorFlowStage :board="board" :period="period" :loading="loading" :data="data" />
    <SectorInsights :summary='summary' :divergence-count='data?.divergences?.length || 0' />

    <section v-if='data?.divergences?.length' class='divergences'>
      <div class='section-title'><h2>价流背离</h2><span>{{ data.divergences.length }} 个板块</span></div>
      <div class='divergence-list'>
        <span v-for='row in data.divergences' :key='row.name'>
          {{ row.name }}
          <b :class='pctClass(row.net)'>{{ signedAmount(row.net) }}</b>
          <b :class='pctClass(row.change_pct)'>{{ formatPct(row.change_pct) }}</b>
        </span>
      </div>
    </section>
    <section class='table-card'>
      <div class='table-title'>
        <div><h2>{{ board === 'industry' ? '行业' : '概念' }}完整明细</h2><span>{{ pagination.total }} 条</span></div>
        <small>{{ period === 'day' ? '当日资金快照' : period === '5d' ? '5日累计（快照优先）' : period === '10d' ? '10日累计（快照优先）' : '20日累计（日度快照）' }}</small>
      </div>
      <div class='table-wrap'>
        <table>
          <thead><tr>
            <th>名称</th>
            <th><button type='button' @click='sortBy(`net`)'>净额 {{ sortMark('net') }}</button></th>
            <th><button type='button' @click='sortBy(`inflow`)'>流入 {{ sortMark('inflow') }}</button></th>
            <th><button type='button' @click='sortBy(`outflow`)'>流出 {{ sortMark('outflow') }}</button></th>
            <th><button type='button' @click='sortBy(`change_pct`)'>涨跌幅 {{ sortMark('change_pct') }}</button></th>
            <th>公司数</th><th>领涨股</th>
            <th><button type='button' @click='sortBy(`leader_pct`)'>领涨% {{ sortMark('leader_pct') }}</button></th>
          </tr></thead>
          <tbody><tr v-for='row in items' :key='row.name'>
            <td class='name'>{{ row.name }}</td>
            <td :class='pctClass(row.net)'>{{ signedAmount(row.net) }}</td>
            <td>{{ formatAmount(row.inflow) }}</td>
            <td>{{ formatAmount(row.outflow) }}</td>
            <td :class='pctClass(row.change_pct)'>{{ formatPct(row.change_pct) }}</td>
            <td>{{ row.company_count ?? '-' }}</td>
            <td>{{ row.leader || '-' }}</td>
            <td :class='pctClass(row.leader_pct)'>{{ formatPct(row.leader_pct) }}</td>
          </tr></tbody>
        </table>
        <div v-if='!items.length && !loading' class='empty'>暂无符合条件的板块</div>
      </div>
      <div class='pager'>
        <span>第 {{ pagination.page }} / {{ pagination.total_pages }} 页</span>
        <div>
          <button type='button' :disabled='pagination.page <= 1 || loading' @click='changePage(-1)'>上一页</button>
          <button type='button' :disabled='pagination.page >= pagination.total_pages || loading' @click='changePage(1)'>下一页</button>
        </div>
      </div>
    </section>
    <MarketDataStatus :meta='data?.meta' />
  </div>
</template>

<style scoped>
.page {
  color: #e7edf4;
}

.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 14px;
}

.title-row,
.header-meta,
.section-title,
.table-title,
.pager {
  display: flex;
  align-items: center;
}

.title-row {
  gap: 10px;
}

.page-header h1 {
  margin: 0;
  font-size: 22px;
}

.page-header p {
  margin: 4px 0 0;
  color: #8592a2;
  font-size: 13px;
}

.live-badge {
  border: 1px solid #2e4251;
  border-radius: 4px;
  color: #8fc6d6;
  font-size: 11px;
  padding: 2px 7px;
}

.header-meta {
  gap: 12px;
  color: #71808f;
  font-size: 12px;
}

button {
  border: 1px solid #2b3947;
  border-radius: 5px;
  background: #121b24;
  color: #9eabb8;
  cursor: pointer;
  font: inherit;
}

button:hover:not(:disabled) {
  border-color: #497087;
  color: #e9f2f7;
}

button:disabled {
  cursor: not-allowed;
  opacity: .38;
}

.icon-button {
  display: grid;
  width: 34px;
  height: 34px;
  padding: 0;
  place-items: center;
  font-size: 18px;
}

.spinning {
  animation: spin .8s linear infinite;
}

.error-box {
  margin-bottom: 12px;
  border: 1px solid #6d3037;
  border-radius: 6px;
  background: #28171c;
  color: #ff9ca4;
  padding: 10px 12px;
}

.control-bar {
  display: grid;
  grid-template-columns: auto 1fr minmax(270px, 360px);
  gap: 18px;
  align-items: center;
  margin-bottom: 10px;
  border-top: 1px solid #26333e;
  border-bottom: 1px solid #26333e;
  padding: 10px 0;
}

.segmented,
.period-tabs,
.search-form {
  display: flex;
  gap: 5px;
}

.segmented button,
.period-tabs button {
  min-height: 30px;
  padding: 5px 11px;
}

.segmented button.active,
.period-tabs button.active {
  border-color: #327495;
  background: #163649;
  color: #e9f7ff;
}

.search-form {
  justify-self: end;
  width: 100%;
}

.search-form input {
  min-width: 0;
  flex: 1;
  border: 1px solid #2b3947;
  border-radius: 5px;
  outline: none;
  background: #0e161e;
  color: #e8eef4;
  padding: 7px 10px;
}

.search-form input:focus {
  border-color: #4680a0;
}

.search-form button {
  padding: 6px 14px;
}

.period-note {
  display: block;
  margin-top: 5px;
  color: #6b7987;
  font-size: 10px;
}

.snapshot-note {
  margin: 10px 0;
  padding: 10px 14px;
  border: 1px solid #4a3c1a;
  border-radius: 6px;
  background: #2a2413;
  color: #d9b96a;
  font-size: 13px;
}

.snapshot-strip {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  margin-bottom: 10px;
  border: 1px solid #26343f;
  border-radius: 7px;
  background: #101820;
}

.snapshot-strip > div {
  padding: 10px 14px;
  border-right: 1px solid #26343f;
}

.snapshot-strip > div:last-child {
  border-right: 0;
}

.snapshot-strip span {
  color: #758391;
  font-size: 11px;
}

.snapshot-strip strong {
  display: block;
  margin-top: 2px;
  font-size: 15px;
}

.divergences,
.table-card {
  margin-bottom: 10px;
  border: 1px solid #26343f;
  border-radius: 8px;
  background: #0c141c;
}

.section-title h2,
.table-title h2 {
  margin: 0;
  font-size: 14px;
}

.divergences {
  padding: 12px 14px;
}

.section-title,
.table-title,
.pager {
  justify-content: space-between;
}

.section-title > span,
.table-title span,
.table-title small {
  color: #71808c;
  font-size: 11px;
}

.divergence-list {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin-top: 9px;
}

.divergence-list > span {
  display: flex;
  gap: 6px;
  align-items: center;
  border-left: 2px solid #4a6070;
  background: #111b23;
  color: #aab5be;
  padding: 6px 9px;
  font-size: 10px;
}

.table-card {
  overflow: hidden;
}

.table-title {
  padding: 11px 13px;
  border-bottom: 1px solid #25323c;
}

.table-title > div {
  display: flex;
  gap: 8px;
  align-items: baseline;
}

.table-wrap {
  max-height: 540px;
  overflow: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

th {
  position: sticky;
  z-index: 2;
  top: 0;
  background: #101922;
  color: #75838f;
  font-weight: 500;
  text-align: right;
  white-space: nowrap;
}

th,
td {
  padding: 9px 10px;
  border-bottom: 1px solid #202c35;
}

th:first-child,
td:first-child {
  text-align: left;
}

th button {
  border: 0;
  background: transparent;
  color: inherit;
  padding: 0;
}

td {
  color: #aab4bd;
  text-align: right;
  white-space: nowrap;
}

tbody tr:hover {
  background: #111e27;
}

td.name {
  color: #e1e8ed;
  font-weight: 600;
}

.empty {
  color: #687681;
  padding: 36px;
  text-align: center;
}

.pager {
  color: #6d7a85;
  padding: 9px 12px;
  font-size: 11px;
}

.pager button {
  margin-left: 6px;
  padding: 5px 10px;
}

.up {
  color: #ec6c74;
}

.down {
  color: #68c29f;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

@media (max-width: 1120px) {
  .control-bar {
    grid-template-columns: auto 1fr;
  }

  .search-form {
    grid-column: 1 / -1;
    justify-self: stretch;
  }
}

@media (max-width: 680px) {
  .page-header {
    align-items: stretch;
  }

  .header-meta {
    flex-direction: column;
    align-items: flex-end;
  }

  .control-bar {
    grid-template-columns: 1fr;
    gap: 8px;
  }

  .period-tabs,
  .segmented {
    overflow-x: auto;
  }

  .search-form {
    grid-column: auto;
  }

  .snapshot-strip {
    grid-template-columns: repeat(2, 1fr);
  }

  .snapshot-strip > div:nth-child(2) {
    border-right: 0;
  }

  .snapshot-strip > div:nth-child(-n + 2) {
    border-bottom: 1px solid #26343f;
  }

  .table-wrap {
    max-height: 480px;
  }
}

@media (max-width: 430px) {
  .page-header {
    flex-direction: column;
  }

  .header-meta {
    flex-direction: row;
    justify-content: space-between;
  }
}
</style>
