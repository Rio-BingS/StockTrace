<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'
import { aiApi } from '../api/stocks.js'
import { aiDisclaimer, aiErrorText, createAiTask, isCanceled } from '../utils/aiRequest.js'

const props = defineProps({
  stockId: { type: [Number, String], required: true },
})

const analysis = ref('')
const disclaimer = ref('')
const loading = ref(false)
const errorMsg = ref('')
// 已等待秒数（AIX-05）：LLM 同步调用可能要几十秒，给用户进度感
const elapsed = ref(0)
let timer = null
const task = createAiTask()

function stopTimer() {
  if (timer) clearInterval(timer)
  timer = null
}

// 取消进行中的请求：中止网络等待，迟到的响应也会被丢弃
function cancel() {
  task.cancel()
  loading.value = false
  stopTimer()
}

// 切换股票时清空上一只的分析结果与进行中的请求，避免串到新股票上
function reset() {
  cancel()
  analysis.value = ''
  disclaimer.value = ''
  errorMsg.value = ''
}

watch(() => props.stockId, reset)
onBeforeUnmount(cancel)

async function run() {
  const { signal, isCurrent } = task.start()
  loading.value = true
  errorMsg.value = ''
  analysis.value = ''
  elapsed.value = 0
  stopTimer()
  timer = setInterval(() => { elapsed.value += 1 }, 1000)
  try {
    const res = await aiApi.analyzeStock(props.stockId, { signal })
    if (!isCurrent()) return
    analysis.value = res.data.analysis
    disclaimer.value = aiDisclaimer(res.data)
  } catch (e) {
    if (!isCurrent() || isCanceled(e)) return
    errorMsg.value = aiErrorText(e)
  } finally {
    if (isCurrent()) {
      loading.value = false
      stopTimer()
    }
  }
}
</script>

<template>
  <div class="ai-panel">
    <div class="ai-head">
      <h3>AI 分析</h3>
      <div class="ai-actions">
        <button v-if="loading" class="btn ghost" @click="cancel">取消</button>
        <button class="btn" :disabled="loading" @click="run">
          {{ loading ? `分析中，已等待 ${elapsed} 秒…` : (analysis ? '重新分析' : '开始分析') }}
        </button>
      </div>
    </div>
    <div aria-live="polite">
      <div v-if="errorMsg" class="error">{{ errorMsg }}</div>
      <pre v-if="analysis" class="analysis">{{ analysis }}</pre>
      <div v-if="analysis" class="disclaimer">{{ disclaimer }}</div>
      <div v-else-if="loading" class="hint">取消只停止等待；已发出的 AI 调用仍会计入当日额度。</div>
      <div v-else-if="!errorMsg" class="hint">
        基于本地落库的行情数据由 AI 解读，需要先在设置页配置 AI 服务商。
      </div>
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
.ai-actions {
  display: flex;
  gap: 8px;
}
.btn.ghost {
  background: transparent;
  color: #e94560;
}
.btn:hover:not(:disabled) {
  background: #d63a54;
}
.btn.ghost:hover {
  background: #3a1a1f;
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
