<script setup>
import { RouterLink } from 'vue-router'
</script>

<template>
  <div class="subnav">
    <RouterLink to="/market" class="back">← 数据</RouterLink>
    <nav class="tabs">
      <RouterLink to="/market/trend">走势</RouterLink>
      <RouterLink to="/market/sectors">板块</RouterLink>
      <RouterLink to="/market/institutions">机构持仓</RouterLink>
      <RouterLink to="/market/national-etf">国家队相关ETF观察</RouterLink>
      <RouterLink to="/market/etf-radar">份额雷达</RouterLink>
    </nav>
  </div>
</template>

<style scoped>
.subnav {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px 16px;
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 1px solid #1a2744;
}

.back {
  color: var(--accent);
  text-decoration: none;
  font-size: 14px;
  font-weight: 600;
  white-space: nowrap;
}

.back:hover {
  text-decoration: underline;
}

.tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.tabs a {
  color: #aaa;
  text-decoration: none;
  padding: 6px 12px;
  border-radius: 6px;
  font-size: 13px;
  background: #1a1a2e;
  border: 1px solid transparent;
}

.tabs a:hover {
  color: #eee;
  border-color: #0f3460;
}

.tabs a.router-link-active {
  color: #fff;
  background: #0f3460;
  border-color: #1a4a7a;
}

@media (max-width: 640px) {
  .tabs a {
    padding: 5px 10px;
    font-size: 12px;
  }
}
</style>
