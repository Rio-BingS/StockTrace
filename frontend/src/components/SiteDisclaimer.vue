<script setup>
import { onMounted, ref } from 'vue'
import { useDialog } from '../composables/useDialog.js'

const STORAGE_KEY = 'stocktrace:disclaimer-ack-v1'

const show = ref(false)

// 焦点管理：打开时聚焦确认按钮、背景 inert、Esc 关闭、关闭后焦点回到触发元素（FEX-04）。
// Esc 只是暂时关闭，不记为已确认，下次访问仍会提示
const { dialogRef, initialFocusRef } = useDialog(show, {
  onEscape: () => {
    show.value = false
  },
})

function dismiss() {
  try {
    localStorage.setItem(STORAGE_KEY, new Date().toISOString())
  } catch {
    /* 隐私模式下存不了也要能关掉 */
  }
  show.value = false
}

onMounted(() => {
  let acked = false
  try {
    acked = Boolean(localStorage.getItem(STORAGE_KEY))
  } catch {
    /* localStorage 不可用（如隐私模式）时保持未确认，每次访问都会提示 */
  }
  if (!acked) show.value = true
})

defineExpose({
  open: () => {
    show.value = true
  },
})
</script>

<template>
  <Teleport to="body">
    <div v-if="show" class="overlay">
      <div
        ref="dialogRef"
        class="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="disclaimer-title"
        tabindex="-1"
      >
        <h2 id="disclaimer-title">免责声明与使用须知</h2>
        <div class="body">
          <p>
            <b>本站为个人自用的 A 股数据监控面板</b
            >，不面向公众提供证券投资咨询服务；运营者非持牌机构，站内一切内容均不构成投资建议、荐股或收益承诺。
          </p>
          <p>
            <b>数据来源与准确性：</b
            >行情与市场数据来自公开第三方接口（东方财富、新浪、腾讯、同花顺、乐咕乐股等），可能存在延迟、缺漏或错误，仅作个人参考，不保证其准确性、完整性与实时性；部分数据（如北向资金）因上游披露调整可能缺失，页面会如实标注「暂不可用」而非估算补齐。
          </p>
          <p>
            <b>AI 生成内容：</b
            >个股分析、选股条件翻译与结果点评由大语言模型自动生成，可能存在错误、遗漏或幻觉，<b>不构成任何投资建议</b>，请勿仅凭
            AI 输出做出交易决策。
          </p>
          <p class="risk">
            股市有风险，投资需谨慎。任何依据本站信息进行的投资操作，风险与后果由操作者自行承担。
          </p>
        </div>
        <div class="actions">
          <button ref="initialFocusRef" class="agree" type="button" @click="dismiss">
            我已阅读并理解
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  background: rgba(5, 8, 18, 0.72);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: 20px;
}

.dialog {
  background: #16213e;
  border: 1px solid #2a3a5e;
  border-radius: 10px;
  max-width: 620px;
  width: 100%;
  max-height: 85vh;
  display: flex;
  flex-direction: column;
  padding: 22px 24px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.5);
}

.dialog h2 {
  margin: 0 0 12px;
  font-size: 18px;
  color: #eee;
}

.body {
  overflow-y: auto;
  font-size: 13px;
  line-height: 1.8;
  color: #b8c2d4;
}

.body p {
  margin: 0 0 10px;
}

.body b {
  color: #dce4f2;
}

.body .risk {
  color: #e8b766;
  border-top: 1px dashed #3a3a5a;
  padding-top: 10px;
  margin-top: 4px;
}

.actions {
  margin-top: 14px;
  display: flex;
  justify-content: flex-end;
}

.agree {
  background: var(--accent-strong);
  color: #fff;
  border: none;
  border-radius: 6px;
  padding: 9px 22px;
  font-size: 14px;
  cursor: pointer;
}

.agree:hover {
  background: var(--accent-strong-hover);
}
</style>
