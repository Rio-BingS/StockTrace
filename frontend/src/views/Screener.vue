<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { aiApi, presetApi, stockApi } from '../api/stocks.js'
import { aiDisclaimer, aiErrorText, createAiTask, isCanceled } from '../utils/aiRequest.js'
import { formatPct, formatNum, formatRate, pctClass } from '../utils/format.js'
import { formatAmount } from '../utils/marketFormat.js'
import { errorMessage } from '../utils/errorMessage.js'
import { confirmDialog } from '../stores/feedback.js'
import {
  buildConditions,
  conditionErrors,
  hasDetail,
  newCondition,
} from '../utils/screenerConditions.js'
import { BOOL_FIELDS, ORDER_OPTIONS } from '../constants/screenerFields.js'
import { extraColumnsFor, fmtCell } from '../utils/screenerColumns.js'
import ScreenerConditionList from '../components/ScreenerConditionList.vue'

const router = useRouter()

// 自然语言选股
const query = ref('')
const aiLoading = ref(false)
// 当前结果由 AI 翻译条件得到时，结果表上方展示后端返回的免责声明（AIX-04）
const resultDisclaimer = ref('')

// 手动条件
const logic = ref('all')
const conditions = ref([newCondition()])
const manualLoading = ref(false)

// 结果
const results = ref([])
const appliedConditions = ref(null)
const resultCount = ref(0)
const errorMsg = ref('')
const notice = ref('')

// AI 点评
const comment = ref('')
const commentDisclaimer = ref('')
const commentLoading = ref(false)
const COMMENT_MAX_ROWS = 50

// AI 请求保护（AIX-05）：新请求取消旧请求并丢弃迟到响应；显示已等待秒数，可取消
const aiTask = createAiTask()
const commentTask = createAiTask()
const aiElapsed = ref(0)
const commentElapsed = ref(0)
const tickers = new Map()

function startTicker(counter) {
  stopTicker(counter)
  counter.value = 0
  tickers.set(
    counter,
    setInterval(() => {
      counter.value += 1
    }, 1000),
  )
}
function stopTicker(counter) {
  clearInterval(tickers.get(counter))
  tickers.delete(counter)
}
function cancelAi() {
  aiTask.cancel()
  aiLoading.value = false
  stopTicker(aiElapsed)
}
function cancelComment() {
  commentTask.cancel()
  commentLoading.value = false
  stopTicker(commentElapsed)
}
onBeforeUnmount(() => {
  cancelAi()
  cancelComment()
})

// 结果表动态附加列：条件与排序里用到、且固定列没展示的指标（逻辑见 utils/screenerColumns，ARC-09）
const extraColumns = computed(() => extraColumnsFor(appliedConditions.value))

// 逐行校验：不完整的行标红并禁用"执行筛选"，不再静默丢弃（FEF-09）
const condErrors = computed(() => conditionErrors(conditions.value, BOOL_FIELDS))
const invalidCount = computed(() => condErrors.value.filter(Boolean).length)

function buildSpec() {
  const conds = buildConditions(conditions.value, BOOL_FIELDS)
  if (!conds) return null
  const spec = { logic: logic.value, conditions: conds }
  if (orderBy.value) {
    spec.order_by = orderBy.value
    spec.order_dir = orderDir.value
  }
  return spec
}

const orderBy = ref('')
const orderDir = ref('desc')

// 预设（保存/加载/删除当前条件组合）
const presets = ref([])
const presetName = ref('')
const selectedPreset = ref('')

async function loadPresets() {
  try {
    presets.value = (await presetApi.list()).data
  } catch (e) {
    console.error('加载预设失败', e)
  }
}

async function savePreset() {
  const name = presetName.value.trim()
  const spec = buildSpec()
  if (!name || !spec) {
    errorMsg.value = !name ? '请输入预设名称' : '请先补全标红的条件'
    return
  }
  try {
    await presetApi.create(name, spec)
    presetName.value = ''
    selectedPreset.value = ''
    notice.value = `预设「${name}」已保存`
    await loadPresets()
  } catch (e) {
    errorMsg.value = errorMessage(e, '保存预设失败')
  }
}

function applyPreset(p) {
  logic.value = p.spec.logic || 'all'
  conditions.value = (p.spec.conditions || []).map((c) =>
    newCondition({
      field: c.field,
      op: c.op,
      value: c.op === 'between' ? c.value?.[0] : c.value,
      value2: c.op === 'between' ? c.value?.[1] : undefined,
    }),
  )
  if (!conditions.value.length) {
    conditions.value = [newCondition()]
  }
  orderBy.value = p.spec.order_by || ''
  orderDir.value = p.spec.order_dir || 'desc'
  notice.value = `已载入预设「${p.name}」，点「执行筛选」运行`
}

function presetKey(p) {
  return p.builtin ? `builtin:${p.name}` : `user:${p.id}`
}

function onPresetSelect(key) {
  const p = presets.value.find((x) => presetKey(x) === key)
  if (p) applyPreset(p)
}

async function deletePreset() {
  if (!selectedPreset.value || selectedPreset.value.startsWith('builtin:')) return
  const p = presets.value.find((x) => presetKey(x) === selectedPreset.value)
  if (!p) return
  const ok = await confirmDialog({
    title: '删除预设',
    message: `删除预设「${p.name}」？`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  try {
    await presetApi.remove(p.id)
    selectedPreset.value = ''
    await loadPresets()
  } catch (e) {
    errorMsg.value = errorMessage(e, '删除预设失败')
  }
}

onMounted(loadPresets)

async function runManual() {
  // 手动结果会替换当前结果：进行中的 AI 选股/点评作废
  cancelAi()
  cancelComment()
  errorMsg.value = ''
  notice.value = ''
  comment.value = ''
  const spec = buildSpec()
  if (!spec) {
    errorMsg.value = '请先补全标红的条件'
    return
  }
  manualLoading.value = true
  try {
    const res = await aiApi.screener(spec)
    results.value = res.data.results
    resultCount.value = res.data.count
    appliedConditions.value = spec
    resultDisclaimer.value = ''
  } catch (e) {
    errorMsg.value = errorMessage(e, '筛选失败')
  } finally {
    manualLoading.value = false
  }
}

async function runAi() {
  if (!query.value.trim()) return
  cancelComment()
  const { signal, isCurrent } = aiTask.start()
  errorMsg.value = ''
  notice.value = ''
  comment.value = ''
  aiLoading.value = true
  startTicker(aiElapsed)
  try {
    const res = await aiApi.screenerAi(query.value.trim(), { signal })
    if (!isCurrent()) return
    results.value = res.data.results
    resultCount.value = res.data.count
    appliedConditions.value = res.data.conditions
    resultDisclaimer.value = aiDisclaimer(res.data)
    notice.value = 'AI 已把需求翻译为可复现的结构化条件并执行'
  } catch (e) {
    if (!isCurrent() || isCanceled(e)) return
    errorMsg.value = aiErrorText(e)
  } finally {
    if (isCurrent()) {
      aiLoading.value = false
      stopTicker(aiElapsed)
    }
  }
}

async function runComment() {
  if (!results.value.length) return
  const { signal, isCurrent } = commentTask.start()
  comment.value = ''
  commentLoading.value = true
  startTicker(commentElapsed)
  try {
    // 后端只认代码并按代码从库里取数，最多 50 只（AIX-01）
    const codes = results.value.slice(0, COMMENT_MAX_ROWS).map((r) => ({ code: r.code }))
    const res = await aiApi.screenerComment((query.value || '自定义条件').slice(0, 200), codes, {
      signal,
    })
    if (!isCurrent()) return
    comment.value = res.data.comment
    commentDisclaimer.value = aiDisclaimer(res.data)
  } catch (e) {
    if (!isCurrent() || isCanceled(e)) return
    errorMsg.value = aiErrorText(e)
  } finally {
    if (isCurrent()) {
      commentLoading.value = false
      stopTicker(commentElapsed)
    }
  }
}

async function addToWatchlist(row) {
  try {
    await stockApi.add(row.code, row.name)
    notice.value = `${row.code} ${row.name} 已加入自选`
  } catch (e) {
    errorMsg.value = errorMessage(e, '加入自选失败')
  }
}

function goDetail(row) {
  // 未入库的股票没有 stock_id，跳过去会是 /stock/undefined（FEF-09）
  if (!hasDetail(row)) return
  router.push(`/stock/${row.stock_id}`)
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h1>条件选股</h1>
      <p class="hint">
        基于自选股池最新日线数据筛选；AI
        只负责把自然语言翻译成可复现的条件，数据全部来自真实落库行情。
      </p>
    </div>

    <div v-if="errorMsg" class="msg error">{{ errorMsg }}</div>
    <div v-if="notice" class="msg ok">{{ notice }}</div>

    <!-- AI 辅助 -->
    <div class="card">
      <h2>AI 辅助选股</h2>
      <div class="ai-row">
        <input
          v-model="query"
          class="ai-input"
          maxlength="200"
          placeholder="用自然语言描述选股条件，如：今日涨幅大于3%且成交额5亿以上，按涨幅从高到低"
          @keyup.enter="runAi"
        />
        <button class="btn primary" :disabled="aiLoading || !query.trim()" @click="runAi">
          {{ aiLoading ? `AI 翻译中… ${aiElapsed} 秒` : 'AI 选股' }}
        </button>
        <button v-if="aiLoading" class="btn" @click="cancelAi">取消</button>
      </div>
      <p v-if="appliedConditions" class="cond-preview mono">
        {{ JSON.stringify(appliedConditions) }}
      </p>
    </div>

    <!-- 手动条件 -->
    <div class="card">
      <h2>手动条件</h2>
      <ScreenerConditionList v-model="conditions" :errors="condErrors" />
      <div class="manual-meta">
        <label>条件关系</label>
        <select v-model="logic">
          <option value="all">全部满足（且）</option>
          <option value="any">任一满足（或）</option>
        </select>
        <label>排序</label>
        <select v-model="orderBy">
          <option v-for="o in ORDER_OPTIONS" :key="o.key" :value="o.key">{{ o.label }}</option>
        </select>
        <select v-model="orderDir" :disabled="!orderBy">
          <option value="desc">降序</option>
          <option value="asc">升序</option>
        </select>
        <button
          class="btn primary"
          :disabled="manualLoading || invalidCount > 0"
          :title="invalidCount ? `有 ${invalidCount} 个条件未填写完整` : ''"
          @click="runManual"
        >
          {{ manualLoading ? '筛选中…' : '执行筛选' }}
        </button>
        <span v-if="invalidCount" class="cond-error">有 {{ invalidCount }} 个条件未填写完整</span>
      </div>
      <div class="preset-row">
        <label>策略 / 预设</label>
        <select v-model="selectedPreset" @change="onPresetSelect(selectedPreset)">
          <option value="" disabled>选择内置策略或已保存的预设</option>
          <option v-for="p in presets" :key="presetKey(p)" :value="presetKey(p)">
            {{ p.builtin ? '★ ' + p.name : p.name }}
          </option>
        </select>
        <button
          class="btn small"
          :disabled="!selectedPreset || selectedPreset.startsWith('builtin:')"
          @click="deletePreset"
        >
          删除所选
        </button>
        <input
          v-model="presetName"
          maxlength="50"
          placeholder="当前条件存为预设（名称）"
          class="preset-name"
        />
        <button class="btn small" @click="savePreset">保存预设</button>
      </div>
    </div>

    <!-- 结果 -->
    <div v-if="results.length" class="card">
      <div class="result-head">
        <h2>筛选结果（{{ resultCount }}）</h2>
        <div class="comment-actions">
          <button v-if="commentLoading" class="btn" @click="cancelComment">取消</button>
          <button class="btn" :disabled="commentLoading" @click="runComment">
            {{ commentLoading ? `AI 点评中… ${commentElapsed} 秒` : 'AI 点评' }}
          </button>
        </div>
      </div>
      <div v-if="results.length > COMMENT_MAX_ROWS" class="hint">
        AI 点评只针对前 {{ COMMENT_MAX_ROWS }} 只
      </div>
      <div v-if="resultDisclaimer" class="disclaimer">
        条件由 AI 翻译生成。{{ resultDisclaimer }}
      </div>
      <!-- 窄屏下表格横向滚动，不撑宽整页（FEX-05） -->
      <div class="table-scroll">
        <table class="result-table">
          <thead>
            <tr>
              <th>代码</th>
              <th>名称</th>
              <th>涨跌幅</th>
              <th>收盘价</th>
              <th>振幅</th>
              <th v-for="col in extraColumns" :key="col.key">{{ col.label }}</th>
              <th>成交额</th>
              <th>数据日期</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in results" :key="r.code">
              <td class="mono">{{ r.code }}</td>
              <td>{{ r.name || '-' }}</td>
              <td :class="pctClass(r.change_pct)">{{ formatPct(r.change_pct) }}</td>
              <td>{{ formatNum(r.close_price) }}</td>
              <td>{{ formatRate(r.high_low_pct) }}</td>
              <td
                v-for="col in extraColumns"
                :key="col.key"
                :class="pctClass(col.bool ? null : r[col.key])"
              >
                {{ fmtCell(r, col) }}
              </td>
              <td>{{ formatAmount(r.turnover) }}</td>
              <td>{{ r.trade_date }}</td>
              <td class="actions">
                <button v-if="hasDetail(r)" class="btn small" @click="goDetail(r)">详情</button>
                <button class="btn small" @click="addToWatchlist(r)">+自选</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="comment" class="comment" aria-live="polite">
        <h3>AI 点评</h3>
        <pre class="comment-text">{{ comment }}</pre>
        <div class="disclaimer">{{ commentDisclaimer }}</div>
      </div>
    </div>
    <div v-else-if="appliedConditions && !results.length" class="card hint">
      没有符合条件的股票。
    </div>
  </div>
</template>

<style scoped>
.page {
  padding: 20px 24px;
  max-width: 1100px;
  margin: 0 auto;
}
.page-header h1 {
  margin: 0 0 4px;
  font-size: 22px;
}
.hint {
  color: #909090;
  font-size: 13px;
}
.card {
  border: 1px solid #2a2a44;
  border-radius: 8px;
  padding: 14px 16px;
  margin-bottom: 16px;
}
.card h2 {
  margin: 0 0 12px;
  font-size: 16px;
}
.msg {
  padding: 10px 14px;
  border-radius: 6px;
  margin-bottom: 12px;
  font-size: 14px;
  white-space: pre-wrap;
  word-break: break-all;
}
.msg.error {
  background: #3a1a1f;
  color: var(--danger);
}
.msg.ok {
  background: #1a3a2a;
  color: #7ee2a8;
}
.ai-row {
  display: flex;
  gap: 8px;
}
.ai-input {
  flex: 1;
  padding: 8px 12px;
  border: 1px solid #3a3a5a;
  border-radius: 6px;
  background: #14142a;
  color: #eee;
  font-size: 14px;
}
.cond-preview {
  margin: 10px 0 0;
  color: #8ab4f8;
  font-size: 12px;
  word-break: break-all;
}
.mono {
  font-family: monospace;
}
.manual-meta select {
  padding: 6px 8px;
  border: 1px solid #3a3a5a;
  border-radius: 6px;
  background: #14142a;
  color: #eee;
  font-size: 13px;
}
.cond-error {
  color: var(--danger);
  font-size: 12px;
}
.manual-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.manual-meta label {
  font-size: 13px;
  color: #aaa;
}
.preset-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid #2a2a44;
}
.preset-row label {
  font-size: 13px;
  color: #aaa;
}
.preset-row select,
.preset-row input {
  padding: 6px 8px;
  border: 1px solid #3a3a5a;
  border-radius: 6px;
  background: #14142a;
  color: #eee;
  font-size: 13px;
}
.preset-row input.preset-name {
  min-width: 200px;
}
.btn {
  padding: 6px 14px;
  border: 1px solid #3a3a5a;
  border-radius: 6px;
  background: transparent;
  color: #ccc;
  cursor: pointer;
  font-size: 13px;
}
.btn:hover:not(:disabled) {
  background: #1a2744;
  color: #fff;
}
.btn.primary {
  background: var(--accent-strong);
  border-color: var(--accent);
  color: #fff;
}
.btn.primary:hover:not(:disabled) {
  background: var(--accent-strong-hover);
}
.btn.small {
  padding: 4px 10px;
  font-size: 12px;
}
.btn:disabled {
  opacity: 0.6;
  cursor: wait;
}
.result-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.result-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
}
.result-table th,
.result-table td {
  text-align: left;
  padding: 8px 10px;
  border-bottom: 1px solid #2a2a44;
}
.actions {
  white-space: nowrap;
}
.comment-actions {
  display: flex;
  gap: 8px;
}
.comment {
  margin-top: 14px;
  border-top: 1px solid #2a2a44;
  padding-top: 10px;
}
.comment h3 {
  margin: 0 0 8px;
  font-size: 14px;
}
.comment-text {
  font-family: inherit;
  font-size: 14px;
  line-height: 1.7;
  color: #ddd;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}
.disclaimer {
  margin-top: 6px;
  font-size: 12px;
  color: #b8923a;
}
</style>
