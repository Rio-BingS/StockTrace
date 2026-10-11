<script setup>
defineProps({
  // [[key, label]]
  options: { type: Array, default: () => [] },
  // [[key, label, reason]] 灰置按钮（如需快照积累的区间）
  disabledOptions: { type: Array, default: () => [] },
  modelValue: { type: String, default: '' },
  loading: { type: Boolean, default: false },
})

defineEmits(['update:modelValue'])
</script>

<template>
  <div class="period-picker">
    <button v-for="[key, label] in options" :key="key" type="button"
            :class="{ active: modelValue === key }" :aria-pressed="modelValue === key ? 'true' : 'false'" :disabled="loading"
            @click="$emit('update:modelValue', key)">{{ label }}</button>
    <button v-for="[key, label, reason] in disabledOptions" :key="key" type="button"
            disabled :title="reason">{{ label }}</button>
  </div>
</template>

<style scoped>
.period-picker { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
button {
  cursor: pointer; border: 1px solid #2b3947; background: #121b24;
  color: #9eabb8; border-radius: 5px; padding: 5px 12px; font-size: 12px; font: inherit;
}
button.active { border-color: #327495; background: #163649; color: #e9f7ff; }
button:disabled { opacity: .45; cursor: not-allowed; }
</style>
