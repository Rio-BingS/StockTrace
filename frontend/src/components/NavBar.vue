<script setup>
import { computed } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

const route = useRoute()
const isWatchlist = computed(() => route.path === '/' || route.path.startsWith('/stock/'))
const isMarket = computed(() => route.path.startsWith('/market'))
const isDetector = computed(() => route.path.startsWith('/detector'))
const isScreener = computed(() => route.path.startsWith('/screener'))
const isSettings = computed(() => route.path.startsWith('/settings'))
</script>

<template>
  <nav class="navbar" aria-label="主导航">
    <div class="nav-brand">
      <RouterLink to="/">StockTrace</RouterLink>
    </div>
    <div class="nav-links">
      <RouterLink
        to="/"
        :class="{ active: isWatchlist }"
        :aria-current="isWatchlist ? 'page' : null"
        >自选</RouterLink
      >
      <RouterLink
        to="/market"
        :class="{ active: isMarket }"
        :aria-current="isMarket ? 'page' : null"
        >数据</RouterLink
      >
      <RouterLink
        to="/detector"
        :class="{ active: isDetector }"
        :aria-current="isDetector ? 'page' : null"
        >探测器</RouterLink
      >
      <RouterLink
        to="/screener"
        :class="{ active: isScreener }"
        :aria-current="isScreener ? 'page' : null"
        >选股</RouterLink
      >
      <RouterLink
        to="/settings"
        :class="{ active: isSettings }"
        :aria-current="isSettings ? 'page' : null"
        >设置</RouterLink
      >
    </div>
  </nav>
</template>

<style scoped>
.navbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 24px;
  background: #1a1a2e;
  color: #fff;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
}

.nav-brand a {
  font-size: 20px;
  font-weight: bold;
  color: var(--accent);
  text-decoration: none;
}

.nav-links {
  display: flex;
  align-items: center;
  gap: 8px;
}

.nav-links a {
  color: #ccc;
  text-decoration: none;
  margin-left: 8px;
  padding: 6px 12px;
  border-radius: 6px;
  transition:
    color 0.2s,
    background 0.2s;
}

.nav-links a:hover {
  color: #fff;
  background: #1a2744;
}

.nav-links a.active {
  color: #fff;
  background: #0f3460;
}

/* 窄屏：导航项不换行、可横向滑动，避免撑出页面宽度（FEX-05） */
@media (max-width: 640px) {
  .navbar {
    padding: 10px 12px;
    gap: 12px;
  }

  .nav-brand a {
    font-size: 17px;
  }

  .nav-links {
    flex: 1;
    min-width: 0;
    justify-content: flex-end;
    overflow-x: auto;
    scrollbar-width: none;
    gap: 2px;
  }

  .nav-links::-webkit-scrollbar {
    display: none;
  }

  .nav-links a {
    margin-left: 0;
    padding: 6px 9px;
    white-space: nowrap;
    flex-shrink: 0;
  }
}
</style>
