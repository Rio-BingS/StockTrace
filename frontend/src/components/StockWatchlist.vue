<script setup>
import { ref, onUnmounted } from 'vue'
import { stockApi } from '../api/stocks.js'
import { errorMessage } from '../utils/errorMessage.js'

const emit = defineEmits(['refresh'])

const keyword = ref('')
const searchResults = ref([])
const searching = ref(false)
const adding = ref(false)
const showDropdown = ref(false)
let debounceTimer = null

async function onInput() {
  if (debounceTimer) clearTimeout(debounceTimer)
  const kw = keyword.value.trim()

  if (kw.length < 2) {
    searchResults.value = []
    showDropdown.value = false
    return
  }

  debounceTimer = setTimeout(async () => {
    searching.value = true
    try {
      const res = await stockApi.search(kw)
      searchResults.value = res.data
      showDropdown.value = searchResults.value.length > 0
    } catch (e) {
      console.error('搜索失败', e)
      searchResults.value = []
    } finally {
      searching.value = false
    }
  }, 300)
}

async function addStock(code, name) {
  adding.value = true
  showDropdown.value = false
  try {
    await stockApi.add(code, name)
    keyword.value = ''
    searchResults.value = []
    emit('refresh')
  } catch (e) {
    if (e.response?.data?.code) {
      alert('该股票已在关注列表中')
    } else {
      alert(errorMessage(e, '添加失败'))
    }
  } finally {
    adding.value = false
  }
}

async function onEnter() {
  const kw = keyword.value.trim()
  if (!kw) return

  // 如果是6位数字，直接按代码添加
  if (/^\d{6}$/.test(kw)) {
    await addStock(kw, '')
    return
  }

  // 否则如果有搜索结果，选第一个
  if (searchResults.value.length > 0) {
    const first = searchResults.value[0]
    await addStock(first.code, first.name)
  }
}

function onBlur() {
  // 延迟关闭下拉，以便点击事件能触发
  setTimeout(() => {
    showDropdown.value = false
  }, 200)
}

function onFocus() {
  if (searchResults.value.length > 0) {
    showDropdown.value = true
  }
}

onUnmounted(() => {
  if (debounceTimer) clearTimeout(debounceTimer)
})
</script>

<template>
  <div class="watchlist-add">
    <h3>添加关注</h3>
    <div class="add-form">
      <div class="search-wrapper">
        <input
          v-model="keyword"
          placeholder="输入代码或名称搜索 (如 000001 或 平安)"
          class="input search-input"
          @input="onInput"
          @keyup.enter="onEnter"
          @focus="onFocus"
          @blur="onBlur"
        />
        <div v-if="showDropdown" class="search-dropdown">
          <div
            v-for="item in searchResults"
            :key="item.code"
            class="dropdown-item"
            @mousedown.prevent="addStock(item.code, item.name)"
          >
            <span class="item-code">{{ item.code }}</span>
            <span class="item-name">{{ item.name }}</span>
          </div>
        </div>
      </div>
      <button
        class="btn btn-add"
        @click="onEnter"
        :disabled="adding || !keyword.trim()"
      >
        {{ adding ? '添加中...' : '添加' }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.watchlist-add {
  background: #16213e;
  border-radius: 8px;
  padding: 16px;
  margin-bottom: 24px;
}

.watchlist-add h3 {
  color: #eee;
  margin-bottom: 12px;
  font-size: 16px;
}

.add-form {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  flex-wrap: wrap;
}

.search-wrapper {
  position: relative;
  flex: 1;
  min-width: 260px;
}

.search-input {
  width: 100%;
}

.input {
  padding: 8px 12px;
  border: 1px solid #333;
  border-radius: 6px;
  background: #1a1a2e;
  color: #eee;
  font-size: 14px;
}

.input:focus {
  outline: none;
  border-color: #e94560;
}

.search-dropdown {
  position: absolute;
  top: 100%;
  left: 0;
  right: 0;
  max-height: 280px;
  overflow-y: auto;
  background: #1a1a2e;
  border: 1px solid #333;
  border-radius: 0 0 6px 6px;
  z-index: 100;
  margin-top: 2px;
}

.dropdown-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  cursor: pointer;
  transition: background 0.15s;
}

.dropdown-item:hover {
  background: #1a2744;
}

.item-code {
  color: #e94560;
  font-weight: bold;
  font-size: 14px;
  min-width: 60px;
}

.item-name {
  color: #ccc;
  font-size: 14px;
}

.btn {
  padding: 8px 20px;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
}

.btn-add {
  background: #0f3460;
  color: #fff;
}

.btn-add:hover {
  background: #1a5276;
}

.btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
</style>
