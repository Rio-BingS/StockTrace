import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import { toastError } from './stores/feedback.js'
import { installGlobalErrorHandlers } from './utils/globalErrors.js'
import { prefetchWhenIdle } from './utils/prefetch.js'
import './style.css'

const app = createApp(App)
app.use(router)
// 组件异常与未处理的 Promise 拒绝统一提示，不再静默失效（FEX-06）
installGlobalErrorHandlers(app, toastError)
app.mount('#app')

// 首页空闲时预取图表库与详情页，首次点进个股/市场页不再等待下载（FEX-07）
prefetchWhenIdle([
  () => import('vue-echarts'),
  () => import('./views/StockDetail.vue'),
])
