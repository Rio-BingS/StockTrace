<script setup>
import { ref } from 'vue'
import { alertApi } from '../api/stocks.js'
import { formatDateTime } from '../utils/format.js'
import { errorMessage } from '../utils/errorMessage.js'
import { usePolling } from '../composables/usePolling.js'

// stocks：看板数据（用于添加规则的股票下拉）
defineProps({
  stocks: { type: Array, default: () => [] },
})

const rules = ref([])
const unreadCount = ref(0)
const events = ref([])
const expanded = ref(false)
const loading = ref(false)
const errorMsg = ref('')

const form = ref({ stock_id: '', rule_type: 'price_above', threshold: '', note: '' })

const RULE_OPTIONS = [
  { value: 'price_above', label: '价格上穿' },
  { value: 'price_below', label: '价格下穿' },
  { value: 'daily_pct_above', label: '日涨幅达到 %' },
  { value: 'daily_pct_below', label: '日跌幅达到 %（填正数）' },
]

async function load() {
  try {
    const [rulesRes, eventsRes] = await Promise.all([
      alertApi.list(),
      alertApi.events(expanded.value ? {} : { unread: 1 }),
    ])
    rules.value = rulesRes.data.items
    unreadCount.value = rulesRes.data.unread_count
    events.value = eventsRes.data
    errorMsg.value = ''
  } catch (e) {
    errorMsg.value = errorMessage(e, '加载提醒数据失败')
  }
}

async function addRule() {
  const f = form.value
  if (!f.stock_id || f.threshold === '' || Number.isNaN(Number(f.threshold))) {
    errorMsg.value = '请选择股票并填写数字阈值'
    return
  }
  loading.value = true
  try {
    await alertApi.create({
      stock_id: Number(f.stock_id),
      rule_type: f.rule_type,
      threshold: Number(f.threshold),
      note: f.note,
    })
    form.value = { stock_id: '', rule_type: f.rule_type, threshold: '', note: '' }
    await load()
  } catch (err) {
    errorMsg.value = errorMessage(err, '创建提醒失败')
  } finally {
    loading.value = false
  }
}

async function toggleRule(rule) {
  try {
    await alertApi.update(rule.id, { is_active: !rule.is_active })
    await load()
  } catch (e) {
    errorMsg.value = errorMessage(e, '更新提醒失败')
  }
}

async function removeRule(rule) {
  if (!confirm(`删除「${rule.code} ${rule.rule_display} ${rule.threshold}」提醒？`)) return
  try {
    await alertApi.remove(rule.id)
    await load()
  } catch (e) {
    errorMsg.value = errorMessage(e, '删除提醒失败')
  }
}

async function markAllRead() {
  try {
    await alertApi.markRead()
    await load()
  } catch (e) {
    errorMsg.value = errorMessage(e, '标记已读失败')
  }
}

function toggleEvents() {
  expanded.value = !expanded.value
  load()
}

// 与看板轮询节奏一致：交易时段 60s 刷新未读数与规则状态，非交易时段 10 分钟（FEF-04）
usePolling(load, { interval: 60000 })
</script>

<template>
  <div class="alert-panel">
    <div class="panel-head">
      <h2>
        价格提醒
        <span v-if="unreadCount" class="badge">{{ unreadCount }}</span>
      </h2>
      <div class="head-actions">
        <button class="btn small" @click="toggleEvents">{{ expanded ? '只看未读' : '查看全部记录' }}</button>
        <button v-if="unreadCount" class="btn small" @click="markAllRead">全部已读</button>
      </div>
    </div>

    <div v-if="errorMsg" class="error-msg">{{ errorMsg }}</div>

    <!-- 新建规则 -->
    <form class="rule-form" @submit.prevent="addRule">
      <select v-model="form.stock_id" aria-label="选择股票">
        <option value="" disabled>选择股票</option>
        <option v-for="s in stocks" :key="s.id" :value="s.id">{{ s.code }} {{ s.name }}</option>
      </select>
      <select v-model="form.rule_type" aria-label="规则类型">
        <option v-for="o in RULE_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
      </select>
      <input v-model="form.threshold" type="number" step="any" placeholder="阈值" aria-label="阈值" />
      <input v-model="form.note" maxlength="100" placeholder="备注（可选）" aria-label="备注" class="note" />
      <button class="btn primary" type="submit" :disabled="loading">+ 添加</button>
    </form>

    <!-- 规则列表 -->
    <table v-if="rules.length" class="rule-table">
      <thead>
        <tr><th>股票</th><th>规则</th><th>阈值</th><th>备注</th><th>状态</th><th>操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rules" :key="r.id">
          <td>{{ r.code }} {{ r.name }}</td>
          <td>{{ r.rule_display }}</td>
          <td>{{ r.threshold }}</td>
          <td class="note-cell">{{ r.note || '-' }}</td>
          <td>
            <button class="btn small" :class="r.is_active ? 'on' : 'off'" @click="toggleRule(r)">
              {{ r.is_active ? '监控中' : '已停用' }}
            </button>
          </td>
          <td><button class="btn small danger" @click="removeRule(r)">删除</button></td>
        </tr>
      </tbody>
    </table>
    <p v-else class="empty-hint">还没有提醒规则。添加后，盘中每 5 分钟和收盘汇总会自动评估，触发记录会显示在这里。</p>

    <!-- 触发记录 -->
    <div v-if="events.length" class="events">
      <h3>{{ expanded ? '最近触发记录' : '未读触发' }}</h3>
      <ul>
        <li v-for="ev in events" :key="ev.id" :class="{ unread: !ev.is_read }">
          <span class="msg">{{ ev.message }}</span>
          <span class="time">{{ formatDateTime(ev.created_at) }}</span>
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.alert-panel {
  background: #16213e;
  border-radius: 8px;
  padding: 16px;
  margin-bottom: 24px;
}

.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.panel-head h2 {
  margin: 0;
  font-size: 16px;
  color: #eee;
  display: flex;
  align-items: center;
  gap: 8px;
}

.badge {
  background: var(--accent-strong);
  color: #fff;
  border-radius: 10px;
  padding: 1px 8px;
  font-size: 12px;
}

.head-actions {
  display: flex;
  gap: 8px;
}

.error-msg {
  color: var(--danger);
  background: #3a1a1f;
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 13px;
  margin-bottom: 10px;
}

.rule-form {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.rule-form select,
.rule-form input {
  padding: 7px 10px;
  border: 1px solid #3a3a5a;
  border-radius: 6px;
  background: #14142a;
  color: #eee;
  font-size: 13px;
}

.rule-form input.note {
  flex: 1;
  min-width: 120px;
}

.rule-form input[aria-label='阈值'] {
  width: 110px;
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

.btn.primary {
  background: var(--accent-strong);
  border-color: var(--accent);
  color: #fff;
}

.btn.small {
  padding: 3px 10px;
  font-size: 12px;
}

.btn.on {
  color: #7ee2a8;
  border-color: #2a5a3f;
}

.btn.off {
  color: #888;
}

.btn.danger {
  color: var(--danger);
  border-color: #5a2a33;
}

.rule-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.rule-table th {
  text-align: left;
  color: #888;
  font-weight: normal;
  padding: 6px 8px;
  border-bottom: 1px solid #2a2a44;
}

.rule-table td {
  padding: 7px 8px;
  color: #ddd;
  border-bottom: 1px solid #1e1e38;
}

.note-cell {
  color: #888;
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.empty-hint {
  color: #888;
  font-size: 13px;
}

.events {
  margin-top: 14px;
}

.events h3 {
  margin: 0 0 8px;
  font-size: 13px;
  color: #aaa;
}

.events ul {
  list-style: none;
  margin: 0;
  padding: 0;
}

.events li {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 7px 10px;
  border-radius: 6px;
  font-size: 13px;
  color: #999;
}

.events li.unread {
  background: #2a1a2e;
  color: #ffd7dc;
}

.events .time {
  color: #777;
  white-space: nowrap;
}
</style>
