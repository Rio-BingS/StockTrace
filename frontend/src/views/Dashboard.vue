<script setup>
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { stockApi, dashboardApi, groupApi } from '../api/stocks.js'
import StockTable from '../components/StockTable.vue'
import StockWatchlist from '../components/StockWatchlist.vue'
import AlertPanel from '../components/AlertPanel.vue'
import { errorMessage } from '../utils/errorMessage.js'
import { usePolling } from '../composables/usePolling.js'
import { createBurstScheduler, createSerialRunner } from '../utils/serialRunner.js'

const router = useRouter()
const dashboardData = ref([])
// 首次加载与后台刷新分离（FEF-05）：只有首屏显示"加载中"，
// 后台刷新保留旧表格（不重建 DOM、输入框不失焦），只在页头显示角标
const loading = ref(true)
const refreshing = ref(false)
const loaded = ref(false)
const lastLoadedAt = ref('')
const loadError = ref('')
const fetchLoading = ref(false)
const fetchMessage = ref('')
let pollTimer = null

// 分组：标签页筛选 + 管理（新增/重命名/删除）
const groups = ref([])
const activeGroup = ref('all')

async function loadGroups() {
  try {
    groups.value = (await groupApi.list()).data
  } catch (e) {
    console.error('加载分组失败', e)
  }
}

const filteredData = computed(() => {
  if (activeGroup.value === 'all') return dashboardData.value
  if (activeGroup.value === 'none') return dashboardData.value.filter(d => d.group_id == null)
  return dashboardData.value.filter(d => d.group_id === Number(activeGroup.value))
})

const ungroupedCount = computed(() => dashboardData.value.filter(d => d.group_id == null).length)

async function addGroup() {
  const name = prompt('新分组名称：')
  if (!name || !name.trim()) return
  try {
    await groupApi.create(name.trim())
    await loadGroups()
  } catch (e) {
    alert(errorMessage(e, '创建分组失败'))
  }
}

async function renameGroup(g) {
  const name = prompt('修改分组名称：', g.name)
  if (!name || !name.trim() || name.trim() === g.name) return
  try {
    await groupApi.update(g.id, { name: name.trim() })
    await Promise.all([loadGroups(), loadDashboard()])
  } catch (e) {
    alert(errorMessage(e, '重命名失败'))
  }
}

async function removeGroup(g) {
  if (!confirm(`删除分组「${g.name}」？组内股票将变回未分组。`)) return
  try {
    await groupApi.remove(g.id)
    if (activeGroup.value === g.id) activeGroup.value = 'all'
    await Promise.all([loadGroups(), loadDashboard()])
  } catch (e) {
    alert(errorMessage(e, '删除分组失败'))
  }
}

// 持仓汇总：只统计填了成本价的股票；数据来自看板真实行情
const portfolio = computed(() => {
  let cost = 0
  let value = 0
  let count = 0
  for (const item of dashboardData.value) {
    if (item.cost_price == null || item.quantity == null || item.close_price == null) continue
    cost += Number(item.cost_price) * Number(item.quantity)
    value += Number(item.close_price) * Number(item.quantity)
    count += 1
  }
  if (!count) return null
  return { cost, value, pnl: value - cost, count }
})

function hhmm(d) {
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

async function doLoadDashboard() {
  if (loaded.value) refreshing.value = true
  else loading.value = true
  try {
    const res = await dashboardApi.get()
    dashboardData.value = res.data
    loadError.value = ''
    lastLoadedAt.value = hhmm(new Date())
  } catch (e) {
    loadError.value = errorMessage(e, '加载看板数据失败')
  } finally {
    loading.value = false
    refreshing.value = false
    loaded.value = true
  }
}

// 轮询、手动操作、添加后补刷都走这里：串行执行，执行中的重复调用合并为一次
const loadDashboard = createSerialRunner(doLoadDashboard)
// 添加自选后后端在后台拉行情：3/8/15s 各补刷一次，重复添加从头重排，卸载时取消
const burstRefresh = createBurstScheduler(loadDashboard, [3000, 8000, 15000])
let fetchMsgTimer = null

function stopPoll() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function pollFetchStatus() {
  try {
    const res = await stockApi.fetchStatus()
    const st = res.data
    if (st.running) {
      fetchLoading.value = true
      fetchMessage.value = st.task === 'all' ? '正在拉取全部股票...' : `正在拉取 ${st.code || ''}...`
      return
    }
    // 任务已结束
    stopPoll()
    fetchLoading.value = false
    if (st.last_status === 'error') {
      fetchMessage.value = st.last_error || '拉取失败'
    } else if (st.last_status === 'success') {
      fetchMessage.value = '拉取完成'
      if (fetchMsgTimer) clearTimeout(fetchMsgTimer)
      fetchMsgTimer = setTimeout(() => {
        fetchMsgTimer = null
        if (fetchMessage.value === '拉取完成') fetchMessage.value = ''
      }, 3000)
    } else {
      fetchMessage.value = ''
    }
    await loadDashboard()
  } catch (e) {
    console.error('查询拉取状态失败', e)
  }
}

async function fetchAllData() {
  fetchLoading.value = true
  fetchMessage.value = '启动拉取...'
  try {
    await stockApi.fetchAll()
    stopPoll()
    pollTimer = setInterval(pollFetchStatus, 3000)
    await pollFetchStatus()
  } catch (e) {
    fetchLoading.value = false
    if (e.response?.status === 409) {
      fetchMessage.value = '已有拉取任务在执行'
      stopPoll()
      pollTimer = setInterval(pollFetchStatus, 3000)
      await pollFetchStatus()
    } else {
      fetchMessage.value = errorMessage(e, '启动拉取失败')
    }
  }
}

async function removeStock(item) {
  try {
    await stockApi.remove(item.id)
    loadDashboard()
  } catch (e) {
    alert(errorMessage(e, '取消关注失败'))
  }
}

function goToDetail(item) {
  router.push(`/stock/${item.id}`)
}

function onWatchlistRefresh() {
  loadDashboard()
  // 添加后后端会后台拉行情，短时多刷几次
  burstRefresh.trigger()
}

// 低配 VPS：看板交易时段 60s 轮询、非交易时段 10 分钟、页面隐藏暂停（FEF-04）；
// 拉取任务另有独立轮询
usePolling(loadDashboard, { interval: 60000 })

onMounted(() => {
  loadGroups()
})

onUnmounted(() => {
  stopPoll()
  burstRefresh.cancel()
  if (fetchMsgTimer) clearTimeout(fetchMsgTimer)
})
</script>

<template>
  <div class="dashboard">
    <div class="dashboard-header">
      <h1>股票监控面板</h1>
      <div class="header-actions">
        <span v-if="portfolio" class="portfolio" :class="portfolio.pnl >= 0 ? 'up' : 'down'">
          持仓{{ portfolio.count }}只 · 盈亏 {{ portfolio.pnl >= 0 ? '+' : '' }}{{ portfolio.pnl.toFixed(2) }}
        </span>
        <span v-if="refreshing" class="refresh-badge">刷新中…</span>
        <span v-else-if="lastLoadedAt" class="updated-at">更新于 {{ lastLoadedAt }}</span>
        <span v-if="fetchMessage" class="fetch-msg">{{ fetchMessage }}</span>
        <button
          class="btn btn-primary"
          @click="fetchAllData"
          :disabled="fetchLoading"
        >
          {{ fetchLoading ? '拉取中...' : '刷新数据' }}
        </button>
      </div>
    </div>

    <StockWatchlist @refresh="onWatchlistRefresh" />

    <AlertPanel :stocks="dashboardData" />

    <div class="group-bar">
      <button :class="{ active: activeGroup === 'all' }" @click="activeGroup = 'all'">
        全部 {{ dashboardData.length }}
      </button>
      <button
        v-for="g in groups"
        :key="g.id"
        :class="{ active: activeGroup === g.id }"
        @click="activeGroup = g.id"
      >
        {{ g.name }} {{ g.stock_count }}
        <span class="g-op" title="重命名" @click.stop="renameGroup(g)">✎</span>
        <span class="g-op danger" title="删除分组" @click.stop="removeGroup(g)">✕</span>
      </button>
      <button v-if="ungroupedCount" :class="{ active: activeGroup === 'none' }" @click="activeGroup = 'none'">
        未分组 {{ ungroupedCount }}
      </button>
      <button class="g-add" @click="addGroup">＋ 新分组</button>
    </div>

    <div v-if="loadError && dashboardData.length" class="load-error">
      {{ loadError }}（当前显示的是上次成功加载的数据）
    </div>

    <StockTable
      :data="filteredData"
      :loading="loading"
      :error="loadError"
      :groups="groups"
      @row-click="goToDetail"
      @ai-click="goToDetail"
      @delete="removeStock"
      @refresh="onWatchlistRefresh"
    />
  </div>
</template>

<style scoped>
.dashboard-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 24px;
}

.dashboard-header h1 {
  font-size: 24px;
  color: #eee;
}

.header-actions {
  display: flex;
  gap: 12px;
  align-items: center;
}

.fetch-msg {
  color: #aaa;
  font-size: 13px;
}

.refresh-badge,
.updated-at {
  color: #8a93a6;
  font-size: 12px;
}

.load-error {
  margin-bottom: 10px;
  color: #ff8a96;
  font-size: 13px;
}

.portfolio {
  font-size: 14px;
  font-weight: bold;
}

.portfolio.up {
  color: #e94560;
}

.portfolio.down {
  color: #00c853;
}

.group-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}

.group-bar button {
  padding: 6px 14px;
  border: 1px solid #2a3a5e;
  border-radius: 16px;
  background: #16213e;
  color: #99a6bd;
  font-size: 13px;
  cursor: pointer;
}

.group-bar button.active {
  background: #0f3460;
  color: #fff;
  border-color: #3a7bd5;
}

.group-bar .g-add {
  border-style: dashed;
}

.g-op {
  margin-left: 6px;
  color: #7ea6d9;
  cursor: pointer;
}

.g-op.danger {
  color: #ff8a96;
}

.btn {
  padding: 8px 20px;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
  transition: background 0.2s;
}

.btn-primary {
  background: #e94560;
  color: #fff;
}

.btn-primary:hover {
  background: #c73a52;
}

.btn-primary:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
</style>
