<script setup>
// 选股手动条件编辑器（ARC-09：从 Screener.vue 抽出的纯视图）。
// conditions 为 v-model 绑定的条件行数组；errors 与行一一对应，'' 表示该行有效。
import { BOOL_FIELDS, FIELDS, OPS } from '../constants/screenerFields.js'
import { newCondition } from '../utils/screenerConditions.js'

const conditions = defineModel({ type: Array, required: true })
defineProps({
  errors: { type: Array, default: () => [] },
})

function addCondition() {
  conditions.value.push(newCondition())
}

function removeCondition(i) {
  conditions.value.splice(i, 1)
}

// 切到布尔字段时运算符固定为 =，默认值"是"
function onFieldChange(c) {
  if (BOOL_FIELDS.has(c.field)) {
    c.op = 'eq'
    c.value = '1'
  }
}
</script>

<template>
  <div class="conds">
    <div v-for="(c, i) in conditions" :key="c.uid" class="cond-row" :class="{ invalid: errors[i] }">
      <select v-model="c.field" aria-label="指标" @change="onFieldChange(c)">
        <option v-for="f in FIELDS" :key="f.key" :value="f.key">{{ f.label }}</option>
      </select>
      <select v-if="!BOOL_FIELDS.has(c.field)" v-model="c.op" class="op" aria-label="运算符">
        <option v-for="o in OPS" :key="o.key" :value="o.key">{{ o.label }}</option>
      </select>
      <span v-else class="op">=</span>
      <select v-if="BOOL_FIELDS.has(c.field)" v-model="c.value" class="val" aria-label="取值">
        <option value="1">是</option>
        <option value="0">否</option>
      </select>
      <input
        v-else
        v-model="c.value"
        type="number"
        class="val"
        placeholder="值"
        aria-label="数值"
      />
      <template v-if="c.op === 'between'">
        <span>~</span>
        <input v-model="c.value2" type="number" class="val" placeholder="上限" aria-label="上限" />
      </template>
      <button class="btn small" :disabled="conditions.length <= 1" @click="removeCondition(i)">
        删
      </button>
      <span v-if="errors[i]" class="cond-error">{{ errors[i] }}</span>
    </div>
    <button class="btn small" @click="addCondition">+ 加条件</button>
  </div>
</template>

<style scoped>
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
  flex-wrap: wrap;
}
.cond-row select,
.cond-row input {
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
.cond-row.invalid input,
.cond-row.invalid select {
  border-color: var(--danger);
}
.cond-error {
  color: var(--danger);
  font-size: 12px;
}
.btn {
  padding: 4px 10px;
  border: 1px solid #3a3a5a;
  border-radius: 6px;
  background: transparent;
  color: #ccc;
  cursor: pointer;
  font-size: 12px;
  align-self: flex-start;
}
.btn:hover:not(:disabled) {
  background: #1a2744;
  color: #fff;
}
.btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
</style>
