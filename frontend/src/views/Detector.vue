<script setup>
// 探测器一级页面：信号列表（可按严重程度筛选）+ 信号详情（维度曲线）+ 板块微变热力图。
// 数据来自本站日度扫描（每交易日 17:15 自动执行），页面只读，不触发扫描。
import { reactive, ref, onMounted, onUnmounted } from 'vue'
import MarketDataStatus from '../components/MarketDataStatus.vue'
import DetectorSignalDetail from '../components/DetectorSignalDetail.vue'
import DetectorHeatmap from '../components/DetectorHeatmap.vue'
import { detectorApi } from '../api/stocks.js'
import { errorMessage, isCanceled } from '../utils/errorMessage.js'
import { createRequestGuard } from '../utils/requestGuard.js'

const loading = ref(false), error = ref('')
const data = ref(null)

// ── 信号筛选 ────────────────────────────────
// 筛选维度与后端 detector/signals 的查询参数一一对应；'' 表示不过滤
const filters = reactive({ severity: '', direction: '', target_kind: '' })
const page = ref(1)
const loadingSignals = ref(false), signalsError = ref('')

// 严重程度分级与后端 signals._severity 的三档一致
const SEVERITY_OPTIONS = [
  ['', '全部程度'], ['high', '高'], ['medium', '中'], ['low', '低'],
]
const DIRECTION_OPTIONS = [['', '全部方向'], ['up', '向上'], ['down', '向下']]
const KIND_OPTIONS = [
  ['', '全部对象'], ['index', '指数'], ['stock', '自选股'], ['industry', '行业'], ['concept', '概念'],
]

const SEVERITY_LABEL = { high: '高', medium: '中', low: '低' }
const STATUS_LABEL = { watching: '观察中', active: '维持中', confirmed: '已兑现', missed: '未兑现' }

// ── 热力图 ────────────────────────────────
const board = ref('industry')
const heatmap = ref(null)
const heatmapLoading = ref(false), heatmapError = ref('')

// ── 维度与迭代状态（默认收起：给想核实「参数怎么来的」的人看） ──
const config = ref(null)
const showConfig = ref(false)
const configLoading = ref(false)

const selectedSignalId = ref(null)

async function load() {
  loading.value = true
  error.value = ''
  try {
    await Promise.all([loadSignals(), loadHeatmap()])
  } finally {
    loading.value = false
  }
}

// 连续切换筛选/翻页/板块时取消旧请求、丢弃晚到的旧响应（FEF-06）
const signalsGuard = createRequestGuard()
const heatmapGuard = createRequestGuard()

async function loadSignals() {
  const req = signalsGuard.next()
  loadingSignals.value = true
  signalsError.value = ''
  try {
    const params = { page: page.value }
    for (const key of Object.keys(filters)) {
      if (filters[key]) params[key] = filters[key]
    }
    const res = await detectorApi.getSignals(params, { signal: req.signal })
    if (req.isLatest()) data.value = res.data
  } catch (e) {
    if (req.isLatest() && !isCanceled(e)) signalsError.value = errorMessage(e, '加载信号列表失败')
  } finally {
    if (req.isLatest()) loadingSignals.value = false
  }
}

function setFilter(key, value) {
  if (filters[key] === value) return
  filters[key] = value
  page.value = 1
  loadSignals()
}

function setPage(delta) {
  const total = data.value?.pagination?.total_pages || 1
  const next = Math.min(Math.max(1, page.value + delta), total)
  if (next === page.value) return
  page.value = next
  loadSignals()
}

async function loadHeatmap() {
  const req = heatmapGuard.next()
  heatmapLoading.value = true
  heatmapError.value = ''
  try {
    const res = await detectorApi.getHeatmap({ board: board.value }, { signal: req.signal })
    if (req.isLatest()) heatmap.value = res.data
  } catch (e) {
    if (req.isLatest() && !isCanceled(e)) heatmapError.value = errorMessage(e, '加载热力图失败')
  } finally {
    if (req.isLatest()) heatmapLoading.value = false
  }
}

function setBoard(value) {
  if (board.value === value) return
  board.value = value
  loadHeatmap()
}

async function toggleConfig() {
  showConfig.value = !showConfig.value
  if (showConfig.value && !config.value) {
    configLoading.value = true
    try {
      config.value = (await detectorApi.getConfig()).data
    } catch (e) {
      // 失败时收起面板并提示，下次点击可重试（FEF-07）
      showConfig.value = false
      alert(errorMessage(e, '加载维度配置失败'))
    } finally {
      configLoading.value = false
    }
  }
}

function toggleDetail(id) {
  // 同一行再点一次收起；换行只保留一个展开详情，避免页面过长
  selectedSignalId.value = selectedSignalId.value === id ? null : id
}

function dirClass(direction) {
  return direction === 'up' ? 'up' : direction === 'down' ? 'down' : ''
}
function dirText(direction) {
  return direction === 'up' ? '↑ 向上' : direction === 'down' ? '↓ 向下' : '—'
}

onMounted(load)
onUnmounted(() => { signalsGuard.abort(); heatmapGuard.abort() })
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <div class="title-line">
          <h1>探测器</h1>
          <span v-if="data?.trade_date" class="date-chip">扫描日 {{ data.trade_date }}</span>
        </div>
        <p>成交量平稳时监测量价、资金与结构的渐进微变，捕捉进入新阶段的早期迹象（每交易日 17:15 自动扫描）</p>
      </div>
      <button class="primary" @click="load" :disabled="loading">{{ loading ? '加载中...' : '刷新' }}</button>
    </div>

    <div v-if="error" class="error-box">{{ error }}</div>
    <MarketDataStatus :meta="data?.meta" fallback="探测器信号为盘面形态识别，非买卖建议。" />

    <!-- 最近一次扫描摘要：让「信号有多少、回看表现如何」一眼可见 -->
    <div v-if="data?.latest_run" class="run-grid">
      <div><label>扫描对象</label><b>{{ data.latest_run.targets_scanned }}</b></div>
      <div><label>平静对象</label><b>{{ data.latest_run.calm_count }}</b></div>
      <div><label>观察中(周期1)</label><b>{{ data.latest_run.signals_new }}</b></div>
      <div><label>维持中(周期≥2)</label><b class="hl">{{ data.latest_run.signals_sustained }}</b></div>
      <div><label>5日回看命中率</label><b>{{ data.latest_run.hit_rate != null ? (data.latest_run.hit_rate * 100).toFixed(0) + '%' : '-' }}</b></div>
    </div>
    <div v-else-if="data && !loadingSignals" class="empty-hint">
      {{ data.meta?.message || '暂无扫描结果：探测器每交易日 17:15 自动运行，也可在服务器执行 python manage.py run_detector_scan 手动触发。' }}
    </div>

    <!-- 信号列表 -->
    <section class="card">
      <div class="card-head">
        <h2>信号列表</h2>
        <div class="filter-groups">
          <div class="chips">
            <button v-for="[value, label] in SEVERITY_OPTIONS" :key="'s' + value"
                    :class="{ on: filters.severity === value }" @click="setFilter('severity', value)">
              {{ label }}
            </button>
          </div>
          <div class="chips">
            <button v-for="[value, label] in DIRECTION_OPTIONS" :key="'d' + value"
                    :class="{ on: filters.direction === value }" @click="setFilter('direction', value)">
              {{ label }}
            </button>
          </div>
          <div class="chips">
            <button v-for="[value, label] in KIND_OPTIONS" :key="'k' + value"
                    :class="{ on: filters.target_kind === value }" @click="setFilter('target_kind', value)">
              {{ label }}
            </button>
          </div>
        </div>
      </div>

      <div v-if="signalsError" class="error-box">{{ signalsError }}</div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>方向</th><th>对象</th><th>名称</th><th>信号日</th>
              <th>强度</th><th>周期</th><th>严重程度</th><th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in data?.items || []" :key="row.id"
                :class="{ selected: selectedSignalId === row.id }" @click="toggleDetail(row.id)">
              <td :class="dirClass(row.direction)">{{ dirText(row.direction) }}</td>
              <td>{{ row.target_kind_display }}</td>
              <td class="name">{{ row.target_name }} <small>{{ row.target_code }}</small></td>
              <td>{{ row.trade_date }}</td>
              <td :class="dirClass(row.direction)">{{ row.score?.toFixed?.(0) ?? row.score }}</td>
              <td>{{ row.cycles }}</td>
              <td><span class="sev" :class="row.severity">{{ SEVERITY_LABEL[row.severity] }}</span></td>
              <td><span class="status" :class="row.status">{{ STATUS_LABEL[row.status] || row.status }}</span></td>
            </tr>
          </tbody>
        </table>
        <div v-if="!loadingSignals && !(data?.items || []).length" class="empty">
          {{ data?.trade_date ? '当前筛选条件下无信号：平静对象须至少 2 个微变维度同向且维持 2 个周期以上才会出现在这里。' : '暂无信号数据' }}
        </div>
      </div>
      <div v-if="(data?.pagination?.total_pages || 1) > 1" class="pager">
        <button @click="setPage(-1)" :disabled="page <= 1">上一页</button>
        <span>{{ data.pagination.page }} / {{ data.pagination.total_pages }}（共 {{ data.pagination.total }} 条）</span>
        <button @click="setPage(1)" :disabled="page >= data.pagination.total_pages">下一页</button>
      </div>
      <p class="hint">点击任意一行查看各微变维度的曲线明细</p>
    </section>

    <!-- 信号详情（维度曲线） -->
    <DetectorSignalDetail v-if="selectedSignalId" :signal-id="selectedSignalId" />

    <!-- 板块微变热力图 -->
    <section class="card">
      <div class="card-head">
        <h2>板块微变热力图</h2>
        <div class="chips">
          <button :class="{ on: board === 'industry' }" @click="setBoard('industry')">行业</button>
          <button :class="{ on: board === 'concept' }" @click="setBoard('concept')">概念</button>
        </div>
      </div>
      <div v-if="heatmapError" class="error-box">{{ heatmapError }}</div>
      <p v-if="heatmap?.meta?.message" class="hint warn-hint">{{ heatmap.meta.message }}</p>
      <div v-if="heatmapLoading" class="empty">加载中...</div>
      <DetectorHeatmap v-else-if="heatmap?.rows?.length" :dims="heatmap.dims" :rows="heatmap.rows" />
      <div v-else-if="!heatmapError" class="empty">暂无板块微变矩阵（板块快照积累后自动出现）</div>
      <p class="hint">红=向上微变，绿=向下微变，灰=该维度数据不可用；仅展示通过平静判定的板块。</p>
    </section>

    <!-- 维度与迭代状态：透明化「权重/阈值从哪来、被自动调成了什么样」 -->
    <section class="card">
      <div class="card-head">
        <h2>维度与迭代状态</h2>
        <button class="ghost" @click="toggleConfig">{{ showConfig ? '收起' : (configLoading ? '加载中...' : '展开') }}</button>
      </div>
      <template v-if="showConfig && config">
        <div class="table-wrap">
          <table>
            <thead><tr><th>维度</th><th>适用对象</th><th>权重</th><th>投票门槛(min)</th><th>状态</th><th>口径说明</th></tr></thead>
            <tbody>
              <tr v-for="dim in config.dims" :key="dim.key">
                <td class="name">{{ dim.name }}</td>
                <td>{{ dim.params && dim.key === 'margin_north' ? '指数' : (dim.key === 'big_order_net' ? '指数/板块' : (dim.key === 'vol_ratio_slope' || dim.key === 'turnover_pct' ? '指数/自选股' : '全部')) }}</td>
                <td>{{ dim.weight?.toFixed?.(2) }}</td>
                <td>{{ dim.params?.min ?? '-' }}</td>
                <td>
                  <span v-if="dim.enabled" class="status active">启用</span>
                  <span v-else class="status missed" :title="dim.disabled_reason">停用</span>
                </td>
                <td class="note-cell">{{ dim.note }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="hint">
          迭代规则：每交易日回看 {{ config.review_rules.lookback_days }} 个交易日前的维持信号（后续交易日凑齐才判定；以次日开盘为基期，
          方向上先达到 {{ config.review_rules.hit_sigma_k }}×σ20 且不利波动未先触及同等幅度记为兑现，σ 不可得时按 {{ config.review_rules.hit_pct }}%），
          命中率与同标的历史基线比较，明显跑赢/跑输的维度权重 ×1.05 / ÷1.05（边界 [{{ config.review_rules.weight_bounds[0] }}, {{ config.review_rules.weight_bounds[1] }}]），
          投票门槛有界微调；每次调整都记录在扫描日志里。
        </p>
        <div v-if="config.runs.length" class="table-wrap">
          <table>
            <thead><tr><th>扫描日</th><th>状态</th><th>扫描/平静</th><th>新增/维持</th><th>回看</th><th>命中率</th><th>备注</th></tr></thead>
            <tbody>
              <tr v-for="run in config.runs" :key="run.trade_date">
                <td>{{ run.trade_date }}</td>
                <td><span class="status" :class="run.status === 'ok' ? 'active' : 'missed'">{{ run.status }}</span></td>
                <td>{{ run.targets_scanned }} / {{ run.calm_count }}</td>
                <td>{{ run.signals_new }} / {{ run.signals_sustained }}</td>
                <td>{{ run.reviewed_count }}</td>
                <td>{{ run.hit_rate != null ? (run.hit_rate * 100).toFixed(0) + '%' : '-' }}</td>
                <td class="note-cell">{{ run.message || '-' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
      <p v-else-if="!showConfig" class="hint">
        委比挂单、板块内分化两个维度因无数据源暂未启用（原因与口径见展开后的说明）。
      </p>
    </section>
  </div>
</template>

<style scoped>
.page { color: #dce4f2; }
.page-header { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 11px; }
.page-header h1 { margin: 0; font-size: 22px; }
.page-header p { margin: 4px 0 0; color: #71809a; font-size: 13px; }
.title-line { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
.title-line span.date-chip { color: #8fc6d6; background: #122736; border: 1px solid #2e4a5e; border-radius: 5px; padding: 3px 7px; font-size: 11px; }
.primary { cursor: pointer; border: 1px solid #286391; background: #174673; color: #fff; border-radius: 6px; padding: 7px 13px; }
.primary:disabled, .ghost:disabled { opacity: .5; }
.ghost { cursor: pointer; border: 1px solid #293b5b; background: #0d1729; color: #aeb9cb; border-radius: 6px; padding: 6px 12px; }
.error-box { background: #3a1520; color: #ff8796; padding: 10px; border-radius: 7px; margin-bottom: 10px; }

.run-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 9px; margin: 10px 0; }
.run-grid > div { background: #111d34; border: 1px solid #213251; border-radius: 8px; padding: 11px; }
.run-grid label { display: block; color: #68758d; font-size: 11px; }
.run-grid b { display: block; margin-top: 5px; font-size: 17px; }
.run-grid b.hl { color: #ffbd6b; }

.card { background: #111d34; border: 1px solid #213251; border-radius: 10px; padding: 12px 14px; margin-bottom: 10px; }
.card-head { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-start; justify-content: space-between; margin-bottom: 10px; }
.card-head h2 { margin: 0; font-size: 15px; color: #e6ecf4; }
.filter-groups { display: flex; flex-wrap: wrap; gap: 8px; }
.chips { display: flex; flex-wrap: wrap; gap: 4px; }
.chips button { cursor: pointer; border: 1px solid #293b5b; background: #0d1729; color: #9eabc1; border-radius: 5px; padding: 4px 10px; font-size: 12px; }
.chips button.on { color: #fff; background: #0f3460; border-color: #1a4a7a; }

.table-wrap { overflow: auto; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { background: #0d192d; color: #7887a1; font-weight: 500; text-align: right; padding: 9px 8px; white-space: nowrap; }
td { padding: 8px; border-bottom: 1px solid #1d2a43; text-align: right; white-space: nowrap; }
th:nth-child(-n+3), td:nth-child(-n+3) { text-align: left; }
tbody tr { cursor: pointer; }
tbody tr:hover, tbody tr.selected { background: #162b49; }
.name { color: #e0e7f1; }
.name small { color: #80736a; font-size: 10px; margin-left: 4px; }
.up { color: #ff8796; }
.down { color: #4cd68a; }
.sev { border-radius: 4px; padding: 2px 7px; font-size: 11px; }
.sev.high { color: #ff8796; background: #3a1520; }
.sev.medium { color: #ffbd6b; background: #382711; }
.sev.low { color: #8f9bb2; background: #16213e; }
.status { border-radius: 4px; padding: 2px 7px; font-size: 11px; }
.status.active { color: #4cd68a; background: #10281d; }
.status.watching { color: #8f9bb2; background: #16213e; }
.status.confirmed { color: #79c7ff; background: #122736; }
.status.missed { color: #ff8796; background: #3a1520; }
.empty { text-align: center; padding: 36px 20px; color: #64718a; }
.empty-hint { background: #16213e; border: 1px dashed #293b5b; color: #8f9bb2; border-radius: 8px; padding: 14px; margin-bottom: 10px; font-size: 13px; line-height: 1.6; }
.pager { display: flex; gap: 12px; align-items: center; justify-content: center; margin-top: 10px; color: #8f9bb2; font-size: 12px; }
.pager button { cursor: pointer; border: 1px solid #293b5b; background: #0d1729; color: #aeb9cb; border-radius: 5px; padding: 4px 12px; }
.pager button:disabled { opacity: .4; cursor: default; }
.hint { margin: 9px 0 0; color: #64718a; font-size: 11px; line-height: 1.6; }
.warn-hint { color: #e8b766; }
.note-cell { white-space: normal; text-align: left; color: #8f9bb2; max-width: 420px; }

@media (max-width: 1000px) {
  .run-grid { grid-template-columns: repeat(2, 1fr); }
}
</style>
