<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { aiApi, presetApi, stockApi } from '../api/stocks.js'
import { aiDisclaimer, aiErrorText, createAiTask, isCanceled } from '../utils/aiRequest.js'
import { formatPct, formatNum, pctClass } from '../utils/format.js'

const router = useRouter()

// 与后端 stocks/ai/screener.py FIELDS 保持一致
const FIELDS = [
  { key: 'change_pct', label: '涨跌幅(%)' },
  { key: 'open_close_pct', label: '日内涨幅(%)' },
  { key: 'high_low_pct', label: '日内振幅(%)' },
  { key: 'close_price', label: '收盘价(元)' },
  { key: 'open_price', label: '开盘价(元)' },
  { key: 'high_price', label: '最高价(元)' },
  { key: 'low_price', label: '最低价(元)' },
  { key: 'volume', label: '成交量(手)' },
  { key: 'turnover', label: '成交额(元)' },
  { key: 'pct_5d', label: '5日涨跌幅(%)' },
  { key: 'pct_10d', label: '10日涨跌幅(%)' },
  { key: 'pct_20d', label: '20日涨跌幅(%)' },
  { key: 'pct_60d', label: '60日涨跌幅(%)' },
  { key: 'volume_ratio', label: '量比(今量/5日均量)' },
  { key: 'turnover_5d_avg', label: '5日日均成交额(元)' },
  { key: 'pct_from_high_20d', label: '距20日最高回撤(%)' },
  { key: 'up_days', label: '连续上涨天数' },
  { key: 'above_ma5', label: '站上5日线' },
  { key: 'above_ma10', label: '站上10日线' },
  { key: 'above_ma20', label: '站上20日线' },
  { key: 'above_ma60', label: '站上60日线' },
  { key: 'ma5_gt_ma10', label: '5日线>10日线' },
  { key: 'ma5_gt_ma20', label: '5日线>20日线' },
  { key: 'new_high_20d', label: '创20日新高' },
  { key: 'new_low_20d', label: '创20日新低' },
  { key: 'new_high_60d', label: '创60日新高' },
  { key: 'new_low_60d', label: '创60日新低' },
  { key: 'ma10_gt_ma20', label: '10日线>20日线' },
  { key: 'macd_dif', label: 'MACD DIF' },
  { key: 'macd_dea', label: 'MACD DEA' },
  { key: 'macd_hist', label: 'MACD柱' },
  { key: 'macd_dif_gt_dea', label: 'DIF在DEA上方' },
  { key: 'macd_golden_cross', label: 'MACD金叉' },
  { key: 'macd_dead_cross', label: 'MACD死叉' },
  { key: 'macd_dif_gt_zero', label: 'DIF在零轴上方' },
  { key: 'kdj_k', label: 'KDJ K值' },
  { key: 'kdj_d', label: 'KDJ D值' },
  { key: 'kdj_j', label: 'KDJ J值' },
  { key: 'kdj_golden_cross', label: 'KDJ金叉' },
  { key: 'kdj_dead_cross', label: 'KDJ死叉' },
  { key: 'rsi_6', label: 'RSI(6)' },
  { key: 'rsi_14', label: 'RSI(14)' },
  { key: 'above_boll_mid', label: '在布林中轨上方' },
  { key: 'above_boll_upper', label: '触及布林上轨' },
  { key: 'below_boll_lower', label: '触及布林下轨' },
  { key: 'turnover_rate', label: '换手率(%)' },
  { key: 'pe_ttm', label: '市盈率TTM(倍)' },
  { key: 'pb', label: '市净率(倍)' },
  { key: 'float_mv', label: '流通市值(元)' },
  { key: 'total_mv', label: '总市值(元)' },
]
// 布尔字段：值只能是 1（是）/ 0（否），与后端 BOOL_FIELDS 保持一致
const BOOL_FIELDS = new Set([
  'above_ma5', 'above_ma10', 'above_ma20', 'above_ma60',
  'ma5_gt_ma10', 'ma5_gt_ma20', 'ma10_gt_ma20',
  'new_high_20d', 'new_low_20d', 'new_high_60d', 'new_low_60d',
  'macd_dif_gt_dea', 'macd_golden_cross', 'macd_dead_cross', 'macd_dif_gt_zero',
  'kdj_golden_cross', 'kdj_dead_cross',
  'above_boll_mid', 'above_boll_upper', 'below_boll_lower',
])
const OPS = [
  { key: 'gt', label: '>' },
  { key: 'gte', label: '>=' },
  { key: 'lt', label: '<' },
  { key: 'lte', label: '<=' },
  { key: 'eq', label: '=' },
  { key: 'between', label: '介于' },
]
const ORDER_OPTIONS = [
  { key: '', label: '默认' },
  ...FIELDS.filter(f => !BOOL_FIELDS.has(f.key)).map(f => ({ key: f.key, label: f.label })),
]

// 自然语言选股
const query = ref('')
const aiLoading = ref(false)
// 当前结果由 AI 翻译条件得到时，结果表上方展示后端返回的免责声明（AIX-04）
const resultDisclaimer = ref('')

// 手动条件
const logic = ref('all')
const conditions = ref([{ field: 'change_pct', op: 'gt', value: '' }])
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
  tickers.set(counter, setInterval(() => { counter.value += 1 }, 1000))
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

// 结果表动态附加列：条件与排序里用到、且固定列没展示的指标
const FIXED_COLS = new Set(['change_pct', 'close_price', 'high_low_pct', 'turnover'])
const extraColumns = computed(() => {
  const spec = appliedConditions.value
  if (!spec) return []
  const keys = []
  for (const c of spec.conditions || []) {
    if (!FIXED_COLS.has(c.field) && !keys.includes(c.field)) keys.push(c.field)
  }
  if (spec.order_by && !FIXED_COLS.has(spec.order_by) && !keys.includes(spec.order_by)) {
    keys.push(spec.order_by)
  }
  return keys.map(k => ({
    key: k,
    label: (FIELDS.find(f => f.key === k) || {}).label || k,
    bool: BOOL_FIELDS.has(k),
  }))
})

function fmtCell(row, col) {
  const v = row[col.key]
  if (v == null) return '-'
  if (col.bool) return v === 1 ? '是' : '否'
  return formatNum(v)
}

function errText(e) {
  const data = e.response?.data
  if (typeof data === 'string') return data
  if (data?.detail) return data.detail
  return e.message || '请求失败'
}

function addCondition() {
  conditions.value.push({ field: 'change_pct', op: 'gt', value: '' })
}
function removeCondition(i) {
  conditions.value.splice(i, 1)
}

function onFieldChange(c) {
  if (BOOL_FIELDS.has(c.field)) {
    c.op = 'eq'
    c.value = '1'
  }
}

function buildSpec() {
  const conds = conditions.value
    .filter(c => c.value !== '' && c.value !== null && !Number.isNaN(Number(c.value)))
    .map(c => ({
      field: c.field,
      op: c.op,
      value: c.op === 'between'
        ? [Number(c.value), Number(c.value2)]
        : Number(c.value),
    }))
    .filter(c => c.op !== 'between' || (!Number.isNaN(c.value[0]) && !Number.isNaN(c.value[1])))
  if (!conds.length) return null
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
    errorMsg.value = !name ? '请输入预设名称' : '请先填写至少一个完整条件'
    return
  }
  try {
    await presetApi.create(name, spec)
    presetName.value = ''
    selectedPreset.value = ''
    notice.value = `预设「${name}」已保存`
    await loadPresets()
  } catch (e) {
    errorMsg.value = '保存预设失败：' + errText(e)
  }
}

function applyPreset(p) {
  logic.value = p.spec.logic || 'all'
  conditions.value = (p.spec.conditions || []).map(c => ({
    field: c.field,
    op: c.op,
    value: c.op === 'between' ? c.value?.[0] : c.value,
    value2: c.op === 'between' ? c.value?.[1] : undefined,
  }))
  if (!conditions.value.length) {
    conditions.value = [{ field: 'change_pct', op: 'gt', value: '' }]
  }
  orderBy.value = p.spec.order_by || ''
  orderDir.value = p.spec.order_dir || 'desc'
  notice.value = `已载入预设「${p.name}」，点「执行筛选」运行`
}

function presetKey(p) {
  return p.builtin ? `builtin:${p.name}` : `user:${p.id}`
}

function onPresetSelect(key) {
  const p = presets.value.find(x => presetKey(x) === key)
  if (p) applyPreset(p)
}

async function deletePreset() {
  if (!selectedPreset.value || selectedPreset.value.startsWith('builtin:')) return
  const p = presets.value.find(x => presetKey(x) === selectedPreset.value)
  if (!p || !confirm(`删除预设「${p.name}」？`)) return
  try {
    await presetApi.remove(p.id)
    selectedPreset.value = ''
    await loadPresets()
  } catch {
    errorMsg.value = '删除预设失败'
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
    errorMsg.value = '请至少填写一个完整条件'
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
    errorMsg.value = errText(e)
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
    const codes = results.value.slice(0, COMMENT_MAX_ROWS).map(r => ({ code: r.code }))
    const res = await aiApi.screenerComment((query.value || '自定义条件').slice(0, 200), codes, { signal })
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
    errorMsg.value = '加入自选失败：' + errText(e)
  }
}

function goDetail(row) {
  router.push(`/stock/${row.stock_id}`)
}

function fmtTurnover(v) {
  if (v == null) return '-'
  if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (v >= 1e4) return (v / 1e4).toFixed(0) + '万'
  return v
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h1>条件选股</h1>
      <p class="hint">基于自选股池最新日线数据筛选；AI 只负责把自然语言翻译成可复现的条件，数据全部来自真实落库行情。</p>
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
      <p v-if="appliedConditions" class="cond-preview mono">{{ JSON.stringify(appliedConditions) }}</p>
    </div>

    <!-- 手动条件 -->
    <div class="card">
      <h2>手动条件</h2>
      <div class="conds">
        <div v-for="(c, i) in conditions" :key="i" class="cond-row">
          <select v-model="c.field" @change="onFieldChange(c)">
            <option v-for="f in FIELDS" :key="f.key" :value="f.key">{{ f.label }}</option>
          </select>
          <select v-if="!BOOL_FIELDS.has(c.field)" v-model="c.op" class="op">
            <option v-for="o in OPS" :key="o.key" :value="o.key">{{ o.label }}</option>
          </select>
          <span v-else class="op">=</span>
          <select v-if="BOOL_FIELDS.has(c.field)" v-model="c.value" class="val">
            <option value="1">是</option>
            <option value="0">否</option>
          </select>
          <input v-else v-model="c.value" type="number" class="val" placeholder="值" />
          <template v-if="c.op === 'between'">
            <span>~</span>
            <input v-model="c.value2" type="number" class="val" placeholder="上限" />
          </template>
          <button class="btn small" :disabled="conditions.length <= 1" @click="removeCondition(i)">删</button>
        </div>
        <button class="btn small" @click="addCondition">+ 加条件</button>
      </div>
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
        <button class="btn primary" :disabled="manualLoading" @click="runManual">
          {{ manualLoading ? '筛选中…' : '执行筛选' }}
        </button>
      </div>
      <div class="preset-row">
        <label>策略 / 预设</label>
        <select v-model="selectedPreset" @change="onPresetSelect(selectedPreset)">
          <option value="" disabled>选择内置策略或已保存的预设</option>
          <option v-for="p in presets" :key="presetKey(p)" :value="presetKey(p)">
            {{ p.builtin ? '★ ' + p.name : p.name }}
          </option>
        </select>
        <button class="btn small" :disabled="!selectedPreset || selectedPreset.startsWith('builtin:')" @click="deletePreset">删除所选</button>
        <input v-model="presetName" maxlength="50" placeholder="当前条件存为预设（名称）" class="preset-name" />
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
      <div v-if="resultDisclaimer" class="disclaimer">条件由 AI 翻译生成。{{ resultDisclaimer }}</div>
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
            <td>{{ formatPct(r.high_low_pct) }}</td>
            <td v-for="col in extraColumns" :key="col.key" :class="pctClass(col.bool ? null : r[col.key])">{{ fmtCell(r, col) }}</td>
            <td>{{ fmtTurnover(r.turnover) }}</td>
            <td>{{ r.trade_date }}</td>
            <td class="actions">
              <button class="btn small" @click="goDetail(r)">详情</button>
              <button class="btn small" @click="addToWatchlist(r)">+自选</button>
            </td>
          </tr>
        </tbody>
      </table>
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
  color: #888;
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
  color: #ff8a96;
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
.conds {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 12px;
}
.cond-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.cond-row select,
.cond-row input,
.manual-meta select {
  padding: 6px 8px;
  border: 1px solid #3a3a5a;
  border-radius: 6px;
  background: #14142a;
  color: #eee;
  font-size: 13px;
}
.cond-row select.op {
  width: 70px;
}
.cond-row input.val {
  width: 120px;
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
  background: #e94560;
  border-color: #e94560;
  color: #fff;
}
.btn.primary:hover:not(:disabled) {
  background: #d63a54;
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
