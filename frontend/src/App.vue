<script setup>
import { ref } from 'vue'
import { RouterView, useRouter } from 'vue-router'
import NavBar from './components/NavBar.vue'
import SiteDisclaimer from './components/SiteDisclaimer.vue'
import ToastHost from './components/ToastHost.vue'
import ConfirmDialog from './components/ConfirmDialog.vue'

// KeepAlive 缓存名单来自路由 meta.keepAlive；页面组件用 defineOptions 显式声明同名，
// 不依赖 SFC 文件名推断的组件名（FEF-15）
// 构建时由 vite define 注入（来源：仓库根目录 VERSION）
const appVersion = __APP_VERSION__

const keepAliveNames = useRouter()
  .getRoutes()
  .filter((r) => r.meta?.keepAlive)
  .map((r) => r.name)

// 首次访问自动弹出；页脚「免责声明」可随时重开
const disclaimerRef = ref(null)

function openDisclaimer() {
  disclaimerRef.value?.open()
}
</script>

<template>
  <div class="app-shell">
    <NavBar />
    <main class="main-content">
      <!-- include 按组件 name 缓存页面：切走不销毁，回来免白屏重拉 -->
      <RouterView v-slot="{ Component }">
        <KeepAlive :include="keepAliveNames">
          <component :is="Component" />
        </KeepAlive>
      </RouterView>
    </main>
    <footer class="site-footer">
      <span>StockTrace v{{ appVersion }} · 个人自用监控面板</span>
      <span class="divider">|</span>
      <span>数据来自公开第三方接口，可能延迟或有错漏；AI 内容自动生成，均不构成投资建议</span>
      <a href="#" class="footer-link" @click.prevent="openDisclaimer">免责声明</a>
    </footer>
    <SiteDisclaimer ref="disclaimerRef" />
    <!-- 全站提示与确认框宿主（FEX-06） -->
    <ToastHost />
    <ConfirmDialog />
  </div>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

.main-content {
  flex: 1;
  max-width: 1400px;
  width: 100%;
  box-sizing: border-box;
  margin: 0 auto;
  padding: 20px;
}

.site-footer {
  max-width: 1400px;
  width: 100%;
  box-sizing: border-box;
  margin: 0 auto;
  padding: 14px 20px 18px;
  border-top: 1px solid #1e2a44;
  color: #8690a4;
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.divider {
  color: #2a3a5e;
}

.footer-link {
  color: #7ea6d9;
  text-decoration: none;
  margin-left: auto;
}

.footer-link:hover {
  text-decoration: underline;
}

@media (max-width: 600px) {
  .site-footer {
    flex-direction: column;
    gap: 4px;
    text-align: center;
  }

  .divider {
    display: none;
  }

  .footer-link {
    margin-left: 0;
  }
}
</style>
