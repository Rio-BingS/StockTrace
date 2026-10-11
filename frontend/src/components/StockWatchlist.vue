<script setup>
import { computed, ref, onUnmounted } from 'vue'
import { stockApi } from '../api/stocks.js'
import { errorMessage, isCanceled } from '../utils/errorMessage.js'
import { createRequestGuard } from '../utils/requestGuard.js'
import { moveHighlight, resolveEnter } from '../utils/combobox.js'
import { toast, toastError } from '../stores/feedback.js'

const emit = defineEmits(['refresh'])

const LISTBOX_ID = 'watchlist-search-listbox'

const keyword = ref('')
const searchResults = ref([])
const searching = ref(false)
// 最近一次搜索已完成的关键词（用于区分"搜索中"与"未找到"）
const searchedKeyword = ref('')
const searchError = ref('')
const adding = ref(false)
const showDropdown = ref(false)
// 键盘高亮的结果下标，-1 表示无高亮（FEF-10）
const highlighted = ref(-1)
const hint = ref('')
let debounceTimer = null
let blurTimer = null
// 序号守卫：丢弃过期的搜索响应，避免旧关键词结果覆盖新结果（FEF-06/FEF-10）
const guard = createRequestGuard()

const activeId = computed(() => (highlighted.value >= 0 ? `${LISTBOX_ID}-opt-${highlighted.value}` : undefined))
const notFound = computed(() =>
  !searching.value && !searchError.value && searchedKeyword.value !== ''
  && searchedKeyword.value === keyword.value.trim() && !searchResults.value.length)

function resetSearch() {
  guard.abort()
  searchResults.value = []
  searching.value = false
  searchedKeyword.value = ''
  searchError.value = ''
  highlighted.value = -1
  showDropdown.value = false
}

function onInput() {
  if (debounceTimer) clearTimeout(debounceTimer)
  hint.value = ''
  const kw = keyword.value.trim()

  if (kw.length < 2) {
    resetSearch()
    return
  }

  highlighted.value = -1
  searching.value = true
  searchError.value = ''
  showDropdown.value = true
  debounceTimer = setTimeout(() => search(kw), 300)
}

async function search(kw) {
  const req = guard.next()
  searching.value = true
  try {
    const res = await stockApi.search(kw, { signal: req.signal })
    if (!req.isLatest()) return
    searchResults.value = res.data
    searchedKeyword.value = kw
    highlighted.value = -1
  } catch (e) {
    if (!req.isLatest() || isCanceled(e)) return
    searchResults.value = []
    searchedKeyword.value = kw
    searchError.value = errorMessage(e, '搜索失败')
  } finally {
    if (req.isLatest()) searching.value = false
  }
}

async function addStock(code, name) {
  adding.value = true
  showDropdown.value = false
  try {
    await stockApi.add(code, name)
    keyword.value = ''
    resetSearch()
    emit('refresh')
  } catch (e) {
    if (e.response?.data?.code) {
      toast('该股票已在关注列表中')
    } else {
      toastError(errorMessage(e, '添加失败'))
    }
  } finally {
    adding.value = false
  }
}

// Enter / 点"添加"：只添加高亮项或完整 6 位代码，不再默认添加第一个结果（FEF-10）
async function onEnter() {
  if (adding.value) return
  const target = resolveEnter({
    keyword: keyword.value,
    results: showDropdown.value ? searchResults.value : [],
    highlighted: showDropdown.value ? highlighted.value : -1,
  })
  if (!target) {
    if (keyword.value.trim()) {
      hint.value = '请用 ↑/↓ 选择一只股票，或输入完整 6 位代码'
      if (searchResults.value.length) showDropdown.value = true
    }
    return
  }
  hint.value = ''
  await addStock(target.code, target.name)
}

function onKeydown(e) {
  if (e.isComposing) return // 输入法组字中的 Enter/方向键不处理
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    if (!searchResults.value.length) return
    e.preventDefault()
    showDropdown.value = true
    highlighted.value = moveHighlight(highlighted.value, e.key === 'ArrowDown' ? 1 : -1, searchResults.value.length)
  } else if (e.key === 'Enter') {
    e.preventDefault()
    onEnter()
  } else if (e.key === 'Escape') {
    if (showDropdown.value) e.preventDefault()
    showDropdown.value = false
    highlighted.value = -1
  }
}

function onBlur() {
  // 延迟关闭下拉，以便点击事件能触发
  if (blurTimer) clearTimeout(blurTimer)
  blurTimer = setTimeout(() => {
    blurTimer = null
    showDropdown.value = false
    highlighted.value = -1
  }, 200)
}

function onFocus() {
  if (keyword.value.trim().length >= 2 && (searchResults.value.length || searching.value || notFound.value)) {
    showDropdown.value = true
  }
}

onUnmounted(() => {
  if (debounceTimer) clearTimeout(debounceTimer)
  if (blurTimer) clearTimeout(blurTimer)
  guard.abort()
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
          role="combobox"
          aria-autocomplete="list"
          :aria-expanded="showDropdown ? 'true' : 'false'"
          :aria-controls="LISTBOX_ID"
          :aria-activedescendant="activeId"
          @input="onInput"
          @keydown="onKeydown"
          @focus="onFocus"
          @blur="onBlur"
        />
        <div v-if="showDropdown" class="search-dropdown">
          <div v-if="searching" class="dropdown-status">搜索中…</div>
          <div v-else-if="searchError" class="dropdown-status error">{{ searchError }}</div>
          <div v-else-if="notFound" class="dropdown-status">
            未找到匹配的股票{{ /^\d{6}$/.test(keyword.trim()) ? '，按 Enter 按代码添加' : '' }}
          </div>
          <div v-else :id="LISTBOX_ID" role="listbox">
            <div
              v-for="(item, i) in searchResults"
              :id="`${LISTBOX_ID}-opt-${i}`"
              :key="item.code"
              class="dropdown-item"
              :class="{ active: i === highlighted }"
              role="option"
              :aria-selected="i === highlighted ? 'true' : 'false'"
              @mouseenter="highlighted = i"
              @mousedown.prevent="addStock(item.code, item.name)"
            >
              <span class="item-code">{{ item.code }}</span>
              <span class="item-name">{{ item.name }}</span>
            </div>
          </div>
        </div>
        <div v-if="hint" class="search-hint">{{ hint }}</div>
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
  border-color: var(--accent);
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

.dropdown-item:hover,
.dropdown-item.active {
  background: #1a2744;
}

.dropdown-status {
  padding: 8px 12px;
  color: #8a93a6;
  font-size: 13px;
}

.dropdown-status.error {
  color: var(--danger);
}

.search-hint {
  margin-top: 4px;
  color: var(--danger);
  font-size: 12px;
}

.item-code {
  color: var(--accent);
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
