<script setup>
import { computed, ref, watch } from 'vue'
import { formatPct, formatNum, formatRate, formatSigned, pctClass } from '../utils/format.js'
import { stockApi } from '../api/stocks.js'
import { computePnl, syncDrafts, validateHolding } from '../utils/holding.js'
import { confirmDialog, toastError } from '../stores/feedback.js'

const props = defineProps({
  data: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  // 加载失败文案：有值时空状态显示错误而不是误导性的"暂无数据"
  error: { type: String, default: '' },
  groups: { type: Array, default: () => [] },
})

const emit = defineEmits(['row-click', 'delete', 'ai-click', 'refresh'])

const expandedRows = ref(new Set())
const costDrafts = ref({})
const savingIds = ref(new Set())
// 持仓校验失败的提示：code → 文案
const costErrors = ref({})

function toggleRow(code) {
  if (expandedRows.value.has(code)) {
    expandedRows.value.delete(code)
  } else {
    expandedRows.value.add(code)
  }
}

function isExpanded(code) {
  return expandedRows.value.has(code)
}

// 盈亏：仅填了成本价的行参与；数据全部来自看板真实行情。
// 预先按 code 计算，模板只读不写（FEF-08）
const pnlMap = computed(() => {
  const map = {}
  for (const item of props.data) map[item.code] = computePnl(item)
  return map
})

// 草稿在数据变化时同步（未编辑的行跟随服务端，编辑中的行保留），
// 不在渲染期间写响应式状态（FEF-08）
watch(
  () => props.data,
  (items) => {
    costDrafts.value = syncDrafts(costDrafts.value, items)
  },
  { immediate: true },
)

function draftOf(item, key) {
  return costDrafts.value[item.code]?.[key] ?? ''
}

function setDraft(item, key, value) {
  const old = costDrafts.value[item.code] || { cost_price: '', quantity: '' }
  costDrafts.value[item.code] = { ...old, [key]: value, dirty: true }
  if (costErrors.value[item.code]) delete costErrors.value[item.code]
}

async function changeGroup(item, value) {
  try {
    await stockApi.update(item.id, { group_id: value === '' ? null : Number(value) })
    emit('refresh')
  } catch {
    toastError('修改分组失败，请重试')
  }
}

async function saveCost(item) {
  const check = validateHolding(costDrafts.value[item.code])
  if (!check.ok) {
    costErrors.value[item.code] = check.error
    return
  }
  savingIds.value.add(item.code)
  try {
    await stockApi.update(item.id, { cost_price: check.cost, quantity: check.qty })
    // 保存成功后取消编辑标记，下一次刷新按服务端值同步
    const d = costDrafts.value[item.code]
    if (d) costDrafts.value[item.code] = { ...d, dirty: false }
    emit('refresh')
  } catch {
    toastError('保存持仓失败，请重试')
  } finally {
    savingIds.value.delete(item.code)
  }
}

function handleGoDetail(e, item) {
  e.stopPropagation()
  emit('row-click', item)
}

function handleAi(e, item) {
  e.stopPropagation()
  emit('ai-click', item)
}

async function handleDelete(e, item) {
  e.stopPropagation()
  const ok = await confirmDialog({
    title: '取消关注',
    message: `确认取消关注 ${item.name || item.code}？`,
    confirmText: '取消关注',
    cancelText: '保留',
    danger: true,
  })
  if (!ok) return
  emit('delete', item)
}
</script>

<template>
  <div class="stock-table-wrapper">
    <div v-if="loading" class="loading">加载中...</div>
    <table v-else class="stock-table">
      <thead>
        <tr>
          <th class="th-expand"></th>
          <th>代码</th>
          <th>名称</th>
          <th>日期</th>
          <th>现价</th>
          <th>涨跌幅</th>
          <th>涨跌价</th>
          <th>持仓盈亏</th>
          <th class="th-actions">操作</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="item in data" :key="item.code">
          <!-- 折叠行（默认显示） -->
          <tr
            class="stock-row"
            :class="{ expanded: isExpanded(item.code) }"
            @click="toggleRow(item.code)"
          >
            <td class="expand-icon">
              <!-- 键盘可达的展开按钮；整行点击仍可展开（FEX-03） -->
              <button
                type="button"
                class="expand-btn"
                :aria-expanded="isExpanded(item.code) ? 'true' : 'false'"
                :aria-label="`${isExpanded(item.code) ? '收起' : '展开'} ${item.name || item.code} 详情`"
                @click.stop="toggleRow(item.code)"
              >
                <span
                  :class="isExpanded(item.code) ? 'arrow-down' : 'arrow-right'"
                  aria-hidden="true"
                ></span>
              </button>
            </td>
            <td class="code">{{ item.code }}</td>
            <td class="name">{{ item.name }}</td>
            <td>{{ item.trade_date || '-' }}</td>
            <!-- 涨跌幅/涨跌价口径：相对昨收（change_pct/change_diff），无昨收时显示 '-' -->
            <td :class="pctClass(item.change_pct)">{{ formatNum(item.close_price) }}</td>
            <td :class="pctClass(item.change_pct)">
              {{ formatPct(item.change_pct) }}
            </td>
            <td :class="pctClass(item.change_diff)">
              {{ formatSigned(item.change_diff) }}
            </td>
            <td :class="pctClass(pnlMap[item.code])">
              {{ formatSigned(pnlMap[item.code]) }}
            </td>
            <td class="actions">
              <button class="btn-kline" @click="handleGoDetail($event, item)" title="查看K线图">
                K线
              </button>
              <button
                class="btn-delete"
                @click="handleDelete($event, item)"
                title="取消关注"
                :aria-label="`取消关注 ${item.name || item.code}`"
              >
                ✕
              </button>
            </td>
          </tr>
          <!-- 展开行 -->
          <tr v-if="isExpanded(item.code)" class="expand-row">
            <td colspan="9">
              <div class="expand-content">
                <div class="expand-grid">
                  <div class="expand-item">
                    <span class="elabel">振幅</span>
                    <!-- 振幅为非负量：不加正号、不着色（FEF-11） -->
                    <span>{{ formatRate(item.high_low_pct) }}</span>
                  </div>
                  <div class="expand-item">
                    <span class="elabel">日内涨幅（较开盘）</span>
                    <span :class="pctClass(item.open_close_pct)">{{
                      formatPct(item.open_close_pct)
                    }}</span>
                  </div>
                  <div class="expand-item">
                    <span class="elabel">昨收</span>
                    <span>{{ formatNum(item.prev_close) }}</span>
                  </div>
                  <div class="expand-item">
                    <span class="elabel">开盘价</span>
                    <span>{{ formatNum(item.open_price) }}</span>
                  </div>
                  <div class="expand-item">
                    <span class="elabel">收盘价</span>
                    <span>{{ formatNum(item.close_price) }}</span>
                  </div>
                  <div class="expand-item">
                    <span class="elabel">最高价</span>
                    <span class="up">{{ formatNum(item.high_price) }}</span>
                  </div>
                  <div class="expand-item">
                    <span class="elabel">最低价</span>
                    <span class="down">{{ formatNum(item.low_price) }}</span>
                  </div>
                </div>
                <div class="expand-actions">
                  <button class="btn-kline-lg" @click="handleGoDetail($event, item)">
                    查看K线图 →
                  </button>
                  <button class="btn-kline-lg btn-ai-lg" @click="handleAi($event, item)">
                    AI 分析
                  </button>
                </div>
                <div class="group-row">
                  <span class="elabel">分组</span>
                  <select
                    :value="item.group_id ?? ''"
                    @change="changeGroup(item, $event.target.value)"
                  >
                    <option value="">未分组</option>
                    <option v-for="g in groups" :key="g.id" :value="g.id">{{ g.name }}</option>
                  </select>
                </div>
                <div class="cost-row">
                  <span class="elabel">持仓（可选，填后参与盈亏统计）</span>
                  <input
                    type="number"
                    step="any"
                    placeholder="成本价"
                    :value="draftOf(item, 'cost_price')"
                    @input="setDraft(item, 'cost_price', $event.target.value)"
                  />
                  <input
                    type="number"
                    step="1"
                    min="0"
                    placeholder="持股数"
                    :value="draftOf(item, 'quantity')"
                    @input="setDraft(item, 'quantity', $event.target.value)"
                  />
                  <button
                    class="btn-kline-lg"
                    :disabled="savingIds.has(item.code)"
                    @click="saveCost(item)"
                  >
                    {{ savingIds.has(item.code) ? '保存中…' : '保存持仓' }}
                  </button>
                  <span v-if="costErrors[item.code]" class="cost-error">{{
                    costErrors[item.code]
                  }}</span>
                </div>
              </div>
            </td>
          </tr>
        </template>
        <tr v-if="!data.length">
          <td v-if="error" colspan="9" class="empty error">{{ error }}</td>
          <td v-else colspan="9" class="empty">暂无数据，请先添加关注的股票并点击"刷新数据"</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.stock-table-wrapper {
  background: #16213e;
  border-radius: 8px;
  overflow-x: auto;
}

.loading {
  text-align: center;
  padding: 40px;
  color: #909090;
}

.stock-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
}

.stock-table th {
  background: #0f3460;
  color: #ccc;
  padding: 12px 16px;
  text-align: left;
  white-space: nowrap;
  font-weight: normal;
}

.th-expand {
  width: 36px;
}

.th-actions {
  width: 100px;
}

.stock-table td {
  padding: 12px 16px;
  color: #ddd;
  white-space: nowrap;
  border-bottom: 1px solid #1a1a2e;
}

.stock-row {
  cursor: pointer;
  transition: background 0.15s;
}

.stock-row:hover {
  background: #1a2744;
}

.stock-row.expanded {
  background: #1a2744;
}

.expand-icon {
  text-align: center;
}

.expand-btn {
  background: transparent;
  border: none;
  padding: 4px 6px;
  cursor: pointer;
  line-height: 1;
}

.expand-btn:focus-visible {
  outline: 2px solid var(--accent);
  border-radius: 4px;
}

.arrow-right::before {
  content: '▶';
  font-size: 10px;
  color: #909090;
}

.arrow-down::before {
  content: '▼';
  font-size: 10px;
  color: #909090;
}

.code {
  color: var(--accent);
  font-weight: bold;
}

.name {
  color: #eee;
}

.actions {
  white-space: nowrap;
}

.up {
  color: var(--up);
  font-weight: bold;
}

.down {
  color: var(--down);
  font-weight: bold;
}

.empty {
  text-align: center;
  color: #919191;
  padding: 40px !important;
}

.cost-error {
  color: var(--danger);
  font-size: 12px;
}

.empty.error {
  color: var(--danger);
}

/* 展开行 */
.expand-row td {
  padding: 0 16px 16px 52px;
  border-bottom: 1px solid #1a1a2e;
}

.expand-content {
  background: #1a1a2e;
  border-radius: 6px;
  padding: 14px 16px;
}

.expand-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 12px 20px;
}

.expand-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.elabel {
  color: #909090;
  font-size: 12px;
}

.expand-item span:last-child {
  font-size: 15px;
  font-weight: bold;
  color: #ddd;
}

.expand-actions {
  margin-top: 12px;
}

.cost-row {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.cost-row .elabel {
  font-size: 12px;
}

.group-row {
  margin-top: 10px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.group-row select {
  padding: 6px 10px;
  border: 1px solid #3a3a5a;
  border-radius: 6px;
  background: #14142a;
  color: #eee;
  font-size: 13px;
}

.cost-row input {
  width: 110px;
  padding: 6px 10px;
  border: 1px solid #3a3a5a;
  border-radius: 6px;
  background: #14142a;
  color: #eee;
  font-size: 13px;
}

/* K线按钮（折叠行内） */
.btn-kline {
  background: transparent;
  border: 1px solid #3a7bd5;
  color: var(--accent);
  border-radius: 4px;
  padding: 2px 8px;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.2s;
  margin-right: 6px;
}

.btn-kline:hover {
  background: #3a7bd5;
  color: #fff;
}

/* K线按钮（展开行） */
.btn-kline-lg {
  background: #0f3460;
  border: 1px solid #3a7bd5;
  color: var(--accent);
  border-radius: 4px;
  padding: 6px 16px;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.2s;
}

.btn-kline-lg:hover {
  background: #3a7bd5;
  color: #fff;
}

/* AI 分析按钮（展开行） */
.btn-ai-lg {
  border-color: #b8923a;
  color: #b8923a;
}

.btn-ai-lg:hover {
  background: #b8923a;
  color: #fff;
}

/* 删除按钮 */
.btn-delete {
  background: transparent;
  border: 1px solid var(--danger);
  color: var(--danger);
  border-radius: 4px;
  padding: 2px 8px;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.2s;
}

.btn-delete:hover {
  background: var(--danger-bg);
  color: var(--danger);
}

/* 移动端适配 */
@media (max-width: 768px) {
  .stock-table th,
  .stock-table td {
    padding: 8px 10px;
    font-size: 13px;
  }

  .expand-row td {
    padding: 0 10px 12px 46px;
  }

  .expand-grid {
    grid-template-columns: repeat(3, 1fr);
    gap: 8px 12px;
  }
}

@media (max-width: 500px) {
  .expand-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
</style>
