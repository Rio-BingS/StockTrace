<script setup>
import { ref, onMounted } from 'vue'
import { useDialog } from '../composables/useDialog.js'
import { aiApi } from '../api/stocks.js'
import { aiErrorText, fieldErrorText } from '../utils/aiRequest.js'

const providers = ref([])
const loading = ref(false)
const errorMsg = ref('')
const notice = ref('')

// 表单状态：编辑时保留 id；api_key 为空表示不修改
const form = ref({ id: null, name: '', base_url: '', model: '', api_key: '', is_enabled: true })
const showForm = ref(false)
const saving = ref(false)
const testingId = ref(null)

// 编辑弹窗焦点管理：打开聚焦首个输入框、背景 inert、Esc 取消、关闭后焦点回到触发按钮（FEX-04）
const { dialogRef, initialFocusRef } = useDialog(showForm, { onEscape: () => { showForm.value = false } })

async function load() {
  loading.value = true
  errorMsg.value = ''
  try {
    const res = await aiApi.listProviders()
    providers.value = res.data.results || res.data
  } catch (e) {
    errorMsg.value = '加载配置失败：' + errText(e)
  } finally {
    loading.value = false
  }
}

// 字段校验错误（base_url / api_key 等）优先展示，其余统一转成字符串，避免 [object Object]
function errText(e) {
  return fieldErrorText(e.response?.data) || aiErrorText(e)
}

function openCreate() {
  form.value = { id: null, name: '', base_url: '', model: '', api_key: '', is_enabled: true }
  showForm.value = true
}

function openEdit(p) {
  form.value = {
    id: p.id,
    name: p.name,
    base_url: p.base_url,
    model: p.model,
    api_key: '',
    is_enabled: p.is_enabled,
  }
  showForm.value = true
}

async function save() {
  saving.value = true
  errorMsg.value = ''
  notice.value = ''
  try {
    const data = {
      name: form.value.name,
      base_url: form.value.base_url,
      model: form.value.model,
      is_enabled: form.value.is_enabled,
    }
    if (form.value.api_key) data.api_key = form.value.api_key
    if (form.value.id) {
      await aiApi.updateProvider(form.value.id, data)
    } else {
      await aiApi.createProvider(data)
    }
    showForm.value = false
    notice.value = '已保存'
    await load()
  } catch (e) {
    errorMsg.value = '保存失败：' + errText(e)
  } finally {
    saving.value = false
  }
}

async function remove(p) {
  if (!confirm(`确定删除「${p.name}」？`)) return
  try {
    await aiApi.deleteProvider(p.id)
    await load()
  } catch (e) {
    errorMsg.value = '删除失败：' + errText(e)
  }
}

async function test(p) {
  testingId.value = p.id
  errorMsg.value = ''
  notice.value = ''
  try {
    const res = await aiApi.testProvider(p.id)
    notice.value = `「${p.name}」连接正常${res.data.reply ? '，回复：' + res.data.reply : ''}`
  } catch (e) {
    errorMsg.value = `「${p.name}」连接失败：` + errText(e)
  } finally {
    testingId.value = null
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-header">
      <h1>设置</h1>
      <p class="hint">配置 AI 服务商（OpenAI 兼容协议，如 DeepSeek / Kimi / Qwen / GLM）。API Key 加密存储，仅显示尾四位。</p>
    </div>

    <div v-if="errorMsg" class="msg error">{{ errorMsg }}</div>
    <div v-if="notice" class="msg ok">{{ notice }}</div>

    <div class="toolbar">
      <button class="btn primary" @click="openCreate">新增服务商</button>
    </div>

    <div v-if="loading" class="hint">加载中…</div>

    <div v-else-if="providers.length" class="table-scroll">
    <table class="provider-table">
      <thead>
        <tr>
          <th>名称</th>
          <th>接口地址</th>
          <th>模型</th>
          <th>API Key</th>
          <th>状态</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="p in providers" :key="p.id">
          <td>{{ p.name }}</td>
          <td class="mono">{{ p.base_url }}</td>
          <td class="mono">{{ p.model }}</td>
          <td class="mono">{{ p.api_key_masked }}</td>
          <td>
            <span :class="p.is_enabled ? 'tag on' : 'tag off'">{{ p.is_enabled ? '启用' : '停用' }}</span>
          </td>
          <td class="actions">
            <button class="btn" :disabled="testingId === p.id" @click="test(p)">
              {{ testingId === p.id ? '测试中…' : '测试' }}
            </button>
            <button class="btn" @click="openEdit(p)">编辑</button>
            <button class="btn danger" @click="remove(p)">删除</button>
          </td>
        </tr>
      </tbody>
    </table>
    </div>
    <div v-else class="hint">还没有配置，点击「新增服务商」添加。</div>

    <!-- Teleport 到 body，背景 #app 才能整体设为 inert（FEX-04） -->
    <Teleport to="body">
    <div v-if="showForm" class="modal-mask" @click.self="showForm = false">
      <div
        ref="dialogRef" class="modal" role="dialog" aria-modal="true"
        aria-labelledby="provider-form-title" tabindex="-1"
      >
        <h2 id="provider-form-title">{{ form.id ? '编辑服务商' : '新增服务商' }}</h2>
        <div class="field">
          <label for="provider-name">名称</label>
          <input id="provider-name" ref="initialFocusRef" v-model="form.name" placeholder="如 DeepSeek" />
        </div>
        <div class="field">
          <label for="provider-base-url">接口地址（base_url）</label>
          <input id="provider-base-url" v-model="form.base_url" placeholder="如 https://api.deepseek.com" />
        </div>
        <div class="field">
          <label for="provider-model">模型名</label>
          <input id="provider-model" v-model="form.model" placeholder="如 deepseek-chat" />
        </div>
        <div class="field">
          <label for="provider-api-key">API Key {{ form.id ? '（留空则不修改）' : '' }}</label>
          <!-- new-password：防止浏览器把站点登录密码自动填进来一并提交（FEF-17） -->
          <input id="provider-api-key" v-model="form.api_key" type="password" autocomplete="new-password" placeholder="sk-..." />
        </div>
        <div class="field row">
          <label class="checkbox">
            <input v-model="form.is_enabled" type="checkbox" /> 启用
          </label>
        </div>
        <div class="modal-actions">
          <button class="btn" @click="showForm = false">取消</button>
          <button class="btn primary" :disabled="saving" @click="save">
            {{ saving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>
    </Teleport>
  </div>
</template>

<style scoped>
.page {
  padding: 20px 24px;
  max-width: 960px;
  margin: 0 auto;
}
.page-header h1 {
  margin: 0 0 4px;
  font-size: 22px;
}
.hint {
  color: #909090;
  font-size: 13px;
}
.msg {
  padding: 10px 14px;
  border-radius: 6px;
  margin: 12px 0;
  font-size: 14px;
  white-space: pre-wrap;
  word-break: break-all;
}
.msg.error {
  background: #3a1a1f;
  color: var(--danger);
}
.msg.ok {
  background: #1a3a2a;
  color: #7ee2a8;
}
.toolbar {
  margin: 12px 0;
}
.provider-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
}
.provider-table th,
.provider-table td {
  text-align: left;
  padding: 8px 10px;
  border-bottom: 1px solid #2a2a44;
}
.mono {
  font-family: monospace;
  font-size: 13px;
}
.tag {
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
}
.tag.on {
  background: #1a3a2a;
  color: #7ee2a8;
}
.tag.off {
  background: #3a3a44;
  color: #999;
}
.actions {
  white-space: nowrap;
}
.btn {
  padding: 6px 12px;
  border: 1px solid #3a3a5a;
  border-radius: 6px;
  background: transparent;
  color: #ccc;
  cursor: pointer;
  font-size: 13px;
  margin-right: 6px;
}
.btn:hover:not(:disabled) {
  background: #1a2744;
  color: #fff;
}
.btn.primary {
  background: var(--accent-strong);
  border-color: var(--accent);
  color: #fff;
}
.btn.primary:hover:not(:disabled) {
  background: #d63a54;
}
.btn.danger {
  color: var(--danger);
  border-color: #5a2a34;
}
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}
.modal {
  background: #1e1e34;
  border-radius: 10px;
  padding: 24px;
  width: 420px;
  max-width: 92vw;
}
.modal h2 {
  margin: 0 0 16px;
  font-size: 18px;
}
.field {
  margin-bottom: 12px;
}
.field label {
  display: block;
  font-size: 13px;
  color: #aaa;
  margin-bottom: 4px;
}
.field input {
  width: 100%;
  box-sizing: border-box;
  padding: 8px 10px;
  border: 1px solid #3a3a5a;
  border-radius: 6px;
  background: #14142a;
  color: #eee;
  font-size: 14px;
}
.field.row .checkbox {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #ccc;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
}
</style>
