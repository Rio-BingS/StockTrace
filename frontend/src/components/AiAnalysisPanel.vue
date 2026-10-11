<script setup>
import { ref } from 'vue'
import { aiApi } from '../api/stocks.js'
import { aiDisclaimer } from '../utils/aiRequest.js'

const props = defineProps({
  stockId: { type: [Number, String], required: true },
})

const analysis = ref('')
const disclaimer = ref('')
const loading = ref(false)
const errorMsg = ref('')

async function run() {
  loading.value = true
  errorMsg.value = ''
  analysis.value = ''
  try {
    const res = await aiApi.analyzeStock(props.stockId)
    analysis.value = res.data.analysis
    disclaimer.value = aiDisclaimer(res.data)
  } catch (e) {
    const data = e.response?.data
    errorMsg.value = data?.detail || data || e.message || '请求失败'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="ai-panel">
    <div class="ai-head">
      <h3>AI 分析</h3>
      <button class="btn" :disabled="loading" @click="run">
        {{ loading ? '分析中，请稍候…' : (analysis ? '重新分析' : '开始分析') }}
      </button>
    </div>
    <div v-if="errorMsg" class="error">{{ errorMsg }}</div>
    <pre v-if="analysis" class="analysis">{{ analysis }}</pre>
    <div v-if="analysis" class="disclaimer">{{ disclaimer }}</div>
    <div v-else-if="!loading && !errorMsg" class="hint">
      基于本地落库的行情数据由 AI 解读，需要先在设置页配置 AI 服务商。
    </div>
  </div>
</template>

<style scoped>
.ai-panel {
  border: 1px solid #2a2a44;
  border-radius: 8px;
  padding: 14px 16px;
  margin-top: 16px;
}
.ai-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.ai-head h3 {
  margin: 0;
  font-size: 16px;
}
.btn {
  padding: 6px 14px;
  border: 1px solid #e94560;
  border-radius: 6px;
  background: #e94560;
  color: #fff;
  cursor: pointer;
  font-size: 13px;
}
.btn:hover:not(:disabled) {
  background: #d63a54;
}
.btn:disabled {
  opacity: 0.6;
  cursor: wait;
}
.error {
  margin-top: 10px;
  padding: 8px 12px;
  border-radius: 6px;
  background: #3a1a1f;
  color: #ff8a96;
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-all;
}
.analysis {
  margin: 12px 0 4px;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: inherit;
  font-size: 14px;
  line-height: 1.7;
  color: #ddd;
}
.disclaimer {
  margin-top: 6px;
  font-size: 12px;
  color: #b8923a;
}
.hint {
  margin-top: 8px;
  font-size: 12px;
  color: #888;
}
</style>
