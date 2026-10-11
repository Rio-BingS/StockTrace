<script setup>
import { ref } from 'vue'
import { formatPct, formatNum, formatSigned, pctClass } from '../utils/format.js'
import { stockApi } from '../api/stocks.js'

defineProps({
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

// 盈亏：仅填了成本价的行参与；数据全部来自看板真实行情
function pnlOf(item) {
  if (item.cost_price == null || item.quantity == null || item.close_price == null) return null
  return (Number(item.close_price) - Number(item.cost_price)) * Number(item.quantity)
}

function draftOf(item, key) {
  if (!(item.code in costDrafts.value)) {
    costDrafts.value[item.code] = {
      cost_price: item.cost_price ?? '',
      quantity: item.quantity ?? '',
    }
  }
  return costDrafts.value[item.code][key]
}

function setDraft(item, key, value) {
  if (!(item.code in costDrafts.value)) {
    costDrafts.value[item.code] = {
      cost_price: item.cost_price ?? '',
      quantity: item.quantity ?? '',
    }
  }
  costDrafts.value[item.code][key] = value
}

async function changeGroup(item, value) {
  try {
    await stockApi.update(item.id, { group_id: value === '' ? null : Number(value) })
    emit('refresh')
  } catch {
    alert('修改分组失败，请重试')
  }
}

async function saveCost(item) {
  const draft = costDrafts.value[item.code] || {}
  const cost = draft.cost_price === '' ? null : Number(draft.cost_price)
  const qty = draft.quantity === '' ? null : Number(draft.quantity)
  if (cost !== null && (Number.isNaN(cost) || cost <= 0)) return
  savingIds.value.add(item.code)
  try {
    await stockApi.update(item.id, { cost_price: cost, quantity: qty })
    emit('refresh')
  } catch {
    alert('保存持仓失败，请重试')
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
  if (!confirm(`确认取消关注 ${item.name || item.code}？`)) return
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
            <td class="expand-icon" @click.stop="toggleRow(item.code)">
              <span :class="isExpanded(item.code) ? 'arrow-down' : 'arrow-right'"></span>
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
            <td :class="pctClass(pnlOf(item))">
              {{ pnlOf(item) == null ? '-' : formatNum(pnlOf(item)) }}
            </td>
            <td class="actions">
              <button class="btn-kline" @click="handleGoDetail($event, item)" title="查看K线图">K线</button>
              <button class="btn-delete" @click="handleDelete($event, item)" title="取消关注">✕</button>
            </td>
          </tr>
          <!-- 展开行 -->
          <tr v-if="isExpanded(item.code)" class="expand-row">
            <td colspan="9">
              <div class="expand-content">
                <div class="expand-grid">
                  <div class="expand-item">
                    <span class="elabel">振幅</span>
                    <span :class="pctClass(item.high_low_pct)">{{ formatPct(item.high_low_pct) }}</span>
                  </div>
                  <div class="expand-item">
                    <span class="elabel">日内涨幅（较开盘）</span>
                    <span :class="pctClass(item.open_close_pct)">{{ formatPct(item.open_close_pct) }}</span>
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
                  <button class="btn-kline-lg" @click="handleGoDetail($event, item)">查看K线图 →</button>
                  <button class="btn-kline-lg btn-ai-lg" @click="handleAi($event, item)">AI 分析</button>
                </div>
                <div class="group-row">
                  <span class="elabel">分组</span>
                  <select :value="item.group_id ?? ''" @change="changeGroup(item, $event.target.value)">
                    <option value="">未分组</option>
                    <option v-for="g in groups" :key="g.id" :value="g.id">{{ g.name }}</option>
                  </select>
                </div>
                <div class="cost-row">
                  <span class="elabel">持仓（可选，填后参与盈亏统计）</span>
                  <input
                    type="number" step="any" placeholder="成本价"
                    :value="draftOf(item, 'cost_price')"
                    @input="setDraft(item, 'cost_price', $event.target.value)"
                  />
                  <input
                    type="number" step="1" placeholder="持股数"
                    :value="draftOf(item, 'quantity')"
                    @input="setDraft(item, 'quantity', $event.target.value)"
                  />
                  <button class="btn-kline-lg" :disabled="savingIds.has(item.code)" @click="saveCost(item)">
                    {{ savingIds.has(item.code) ? '保存中…' : '保存持仓' }}
                  </button>
                </div>
              </div>
            </td>
          </tr>
        </template>
        <tr v-if="!data.length">
          <td v-if="error" colspan="8" class="empty error">{{ error }}</td>
          <td v-else colspan="8" class="empty">暂无数据，请先添加关注的股票并点击"刷新数据"</td>
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
  color: #888;
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
  cursor: pointer;
}

.arrow-right::before {
  content: '▶';
  font-size: 10px;
  color: #888;
}

.arrow-down::before {
  content: '▼';
  font-size: 10px;
  color: #888;
}

.code {
  color: #e94560;
  font-weight: bold;
}

.name {
  color: #eee;
}

.actions {
  white-space: nowrap;
}

.up {
  color: #e94560;
  font-weight: bold;
}

.down {
  color: #00c853;
  font-weight: bold;
}

.empty {
  text-align: center;
  color: #666;
  padding: 40px !important;
}

.empty.error {
  color: #ff8a96;
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
  color: #888;
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
  color: #3a7bd5;
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
  color: #3a7bd5;
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
  border: 1px solid #e94560;
  color: #e94560;
  border-radius: 4px;
  padding: 2px 8px;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.2s;
}

.btn-delete:hover {
  background: #e94560;
  color: #fff;
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
