import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端端口可用环境变量覆盖（默认 8000），例如本机 8000 被其他服务占用时：
// VITE_API_TARGET=http://localhost:8001 npm run dev
const apiTarget = process.env.VITE_API_TARGET || 'http://localhost:8000'

export default defineConfig({
  plugins: [vue()],
  build: {
    rolldownOptions: {
      output: {
        // 框架单独成 vendor chunk：业务代码发版时这块缓存不失效（FEX-07）。
        // ECharts 不合并成一个大块：各页面只用到部分图表类型，保持按需拆分更小
        codeSplitting: {
          groups: [
            { name: 'vendor-vue', test: /node_modules[\\/](vue|@vue|vue-router|axios)[\\/]/ },
          ],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
})
