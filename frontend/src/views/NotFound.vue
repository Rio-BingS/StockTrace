<script setup>
import { RouterLink, useRoute } from 'vue-router'

// 未匹配的地址兜底页（FEF-16）
const route = useRoute()
</script>

<template>
  <div class="not-found">
    <h1>页面不存在</h1>
    <p>
      没有找到 <code>{{ route.fullPath }}</code> 对应的页面，可能是链接有误或页面已调整。
    </p>
    <RouterLink to="/" class="home-link">返回自选面板</RouterLink>
  </div>
</template>

<style scoped>
.not-found {
  text-align: center;
  padding: 80px 20px;
  color: var(--text-secondary);
}

.not-found h1 {
  color: var(--text-primary);
  font-size: 24px;
  margin-bottom: 12px;
}

.not-found code {
  color: var(--text-primary);
  word-break: break-all;
}

.home-link {
  display: inline-block;
  margin-top: 20px;
}
</style>
