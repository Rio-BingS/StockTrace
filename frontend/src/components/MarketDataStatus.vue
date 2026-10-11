<script setup>
import { computed } from 'vue'

const props = defineProps({
  meta: { type: Object, default: () => ({}) },
  fallback: { type: String, default: '' },
})

const cacheLabel = computed(() => ({
  fresh: '缓存有效',
  stale: '使用历史缓存',
  unavailable: '数据不可用',
}[props.meta?.cache_status] || props.meta?.cache_status || ''))
</script>

<template>
  <div v-if="meta?.source || meta?.disclaimer || fallback" class="data-status" :class="meta?.cache_status">
    <div class="status-line">
      <span v-if="meta?.source">来源：{{ meta.source }}</span>
      <span v-if="meta?.source_data_date">数据日期：{{ meta.source_data_date }}</span>
      <span v-if="meta?.data_as_of">数据截至交易日：{{ meta.data_as_of }}</span>
      <span v-if="meta?.fetched_at">获取：{{ meta.fetched_at }}</span>
      <span v-if="cacheLabel" class="badge">{{ cacheLabel }}</span>
    </div>
    <p>{{ meta?.disclaimer || fallback }}</p>
  </div>
</template>

<style scoped>
.data-status { border: 1px solid #243554; background: #101b31; border-radius: 8px; padding: 9px 12px; color: #8f9bb2; font-size: 12px; }
.status-line { display: flex; flex-wrap: wrap; gap: 6px 14px; }
.badge { color: #79c7ff; }
.stale { border-color: #604a28; background: #211b15; }
.stale .badge { color: #f0bd71; }
.unavailable { border-color: #5f2834; background: #2b151c; }
.unavailable .badge { color: var(--danger); }
p { margin: 5px 0 0; line-height: 1.5; }
</style>