<script setup>
import { computed, ref, watch } from 'vue'
import { feedback, resolveDialog } from '../stores/feedback.js'
import { useDialog } from '../composables/useDialog.js'

// 全站确认 / 输入对话框宿主，替代原生 confirm / prompt（FEX-06）
const current = computed(() => feedback.dialog)
const open = computed(() => !!current.value)
const inputValue = ref('')
const inputRef = ref(null)
const confirmBtnRef = ref(null)

function cancel() {
  resolveDialog(current.value?.kind === 'prompt' ? null : false)
}

function confirm() {
  resolveDialog(current.value?.kind === 'prompt' ? inputValue.value : true)
}

// 输入框优先聚焦；确认框聚焦确认按钮
const { dialogRef, focusInitial } = useDialog(open, {
  onEscape: cancel,
  initialFocus: () => (current.value?.kind === 'prompt' ? inputRef.value : confirmBtnRef.value),
})

watch(current, (d, old) => {
  if (d?.kind === 'prompt') inputValue.value = d.defaultValue ?? ''
  // 排队的下一个对话框接替显示时，重新聚焦
  if (d && old && d.id !== old.id) focusInitial()
})
</script>

<template>
  <Teleport to="body">
    <div v-if="current" class="overlay" @click.self="cancel">
      <div
        ref="dialogRef" class="dialog" role="dialog" aria-modal="true"
        :aria-labelledby="`dlg-title-${current.id}`" tabindex="-1"
      >
        <h2 :id="`dlg-title-${current.id}`">{{ current.title }}</h2>
        <p v-if="current.message" class="message">{{ current.message }}</p>
        <form @submit.prevent="confirm">
          <input
            v-if="current.kind === 'prompt'" ref="inputRef" v-model="inputValue"
            class="input" :placeholder="current.placeholder" :aria-label="current.title"
          />
          <div class="actions">
            <button type="button" class="btn" @click="cancel">{{ current.cancelText }}</button>
            <button ref="confirmBtnRef" type="submit" class="btn primary" :class="{ danger: current.danger }">
              {{ current.confirmText }}
            </button>
          </div>
        </form>
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
  z-index: 1050;
  padding: 20px;
}

.dialog {
  width: 100%;
  max-width: 420px;
  background: #16213e;
  border: 1px solid #2a3a5e;
  border-radius: 10px;
  padding: 20px;
  color: var(--text-primary);
}

h2 {
  font-size: 17px;
  margin-bottom: 10px;
}

.message {
  color: var(--text-secondary);
  font-size: 14px;
  line-height: 1.6;
  margin-bottom: 14px;
  word-break: break-word;
}

.input {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid #3a3a5a;
  border-radius: 6px;
  background: #14142a;
  color: #eee;
  font-size: 14px;
  margin-bottom: 14px;
}

.input:focus {
  outline: none;
  border-color: var(--accent);
}

.actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

.btn {
  padding: 7px 16px;
  border: 1px solid #3a4a6e;
  border-radius: 6px;
  background: transparent;
  color: var(--text-primary);
  cursor: pointer;
  font-size: 14px;
}

.btn.primary {
  background: var(--accent-strong);
  border-color: var(--accent-strong);
  color: #fff;
}

.btn.primary.danger {
  background: #b4441f;
  border-color: #b4441f;
}

.btn:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
</style>
