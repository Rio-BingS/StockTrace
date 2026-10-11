import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import { toastError } from './stores/feedback.js'
import { installGlobalErrorHandlers } from './utils/globalErrors.js'
import './style.css'

const app = createApp(App)
app.use(router)
// 组件异常与未处理的 Promise 拒绝统一提示，不再静默失效（FEX-06）
installGlobalErrorHandlers(app, toastError)
app.mount('#app')
