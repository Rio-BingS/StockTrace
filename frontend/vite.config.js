import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { readFileSync } from 'node:fs'

// 应用版本号：唯一来源是仓库根目录的 VERSION 文件，构建时注入为 __APP_VERSION__
let appVersion = 'unknown'
try {
  appVersion = readFileSync(new URL('../VERSION', import.meta.url), 'utf-8').trim() || 'unknown'
} catch {
  // 单独拷贝 frontend/ 构建时没有 VERSION，显示 unknown
}

// 后端端口可用环境变量覆盖（默认 8000），例如本机 8000 被其他服务占用时：
// VITE_API_TARGET=http://localhost:8001 npm run dev
const apiTarget = process.env.VITE_API_TARGET || 'http://localhost:8000'

export default defineConfig({
  plugins: [vue()],
  define: {
    __APP_VERSION__: JSON.stringify(appVersion),
  },
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
