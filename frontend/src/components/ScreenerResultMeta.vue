<script setup>
import { computed } from 'vue'
import { excludedLine, excludedSummary } from '../utils/screenerMeta.js'

// 选股结果的横截面元信息（ALG-12）：数据截至日 + 未参与筛选的股票（可折叠）
// meta 结构见 utils/screenerMeta.js pickScreenerMeta
const props = defineProps({
  meta: { type: Object, default: null },
})

const asOf = computed(() => props.meta?.asOf || null)
const excluded = computed(() => props.meta?.excluded || [])
const summary = computed(() => excludedSummary(excluded.value, asOf.value))
</script>

<template>
  <div v-if="asOf || excluded.length" class="result-meta">
    <p v-if="asOf" class="as-of">
      数据截至 <b>{{ asOf }}</b
      >（自选股最近已收盘交易日，全部结果按这一天的日线计算）
    </p>
    <!-- 原生 details：默认折叠，键盘与读屏可用，不需要额外状态 -->
    <details v-if="excluded.length" class="excluded">
      <summary>{{ summary }}</summary>
      <ul>
        <li v-for="item in excluded" :key="item.stock_id ?? item.code">
          {{ excludedLine(item) }}
        </li>
      </ul>
    </details>
  </div>
</template>

<style scoped>
.result-meta {
  margin: 0 0 10px;
  font-size: 13px;
  color: #aab4c8;
}
.as-of {
  margin: 0 0 6px;
}
.as-of b {
  color: #e6ecf7;
  font-family: monospace;
}
.excluded summary {
  cursor: pointer;
  color: #d6a13c;
}
.excluded ul {
  margin: 6px 0 0;
  padding-left: 20px;
  max-height: 200px;
  overflow-y: auto;
}
.excluded li {
  line-height: 1.7;
}
</style>
