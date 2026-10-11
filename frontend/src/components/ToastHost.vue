<script setup>
import { dismissToast, feedback } from '../stores/feedback.js'
</script>

<template>
  <!-- 非阻塞提示区（FEX-06）：错误用 alert 角色立即播报，其余 polite -->
  <div class="toast-host" aria-live="polite">
    <div
      v-for="t in feedback.toasts"
      :key="t.id"
      class="toast"
      :class="t.type"
      :role="t.type === 'error' ? 'alert' : 'status'"
    >
      <span class="msg">{{ t.message }}</span>
      <button type="button" class="close" aria-label="关闭提示" @click="dismissToast(t.id)">
        ✕
      </button>
    </div>
  </div>
</template>

<style scoped>
.toast-host {
  position: fixed;
  right: 16px;
  bottom: 16px;
  z-index: 1100;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: min(420px, calc(100vw - 32px));
}

.toast {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  background: #1a2744;
  border: 1px solid #2a3a5e;
  border-left: 4px solid var(--accent);
  color: var(--text-primary);
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 13px;
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.35);
}

.toast.error {
  border-left-color: var(--danger);
}

.toast.success {
  border-left-color: var(--ok);
}

.msg {
  flex: 1;
  word-break: break-word;
}

.close {
  background: none;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 12px;
}
</style>
