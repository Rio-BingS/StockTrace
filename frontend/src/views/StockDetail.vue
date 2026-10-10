<script setup>
import { ref, onMounted, computed, watch } from 'vue'
import { useRoute } from 'vue-router'
import { stockApi, marketApi } from '../api/stocks.js'
import { formatPct, formatNum, formatDateTime, pctClass } from '../utils/format.js'
import KlineChart from '../components/KlineChart.vue'
import IntradayChart from '../components/IntradayChart.vue'
import AiAnalysisPanel from '../components/AiAnalysisPanel.vue'

const route = useRoute()
const stockId = computed(() => route.params.id)
const code = ref('')
const stockInfo = ref(null)

const dailyData = ref([])
const latestQuote = ref(null)
const minuteData = ref([])
const loading = ref(false)
const margin = ref(null)
const days = ref(30)

// 当前走势图显示的日期和对应日K数据
const intradayDate = ref('')
const intradayQuote = ref(null)

async function loadMargin() {
  margin.value = null
  const c = code.value
  if (!c) return
  try {
    const data = (await marketApi.getStockMargin(c)).data
    // 切换股票后旧请求晚到时丢弃
    if (code.value === c) margin.value = data
  } catch (e) {
    console.error(e)
  }
}

async function loadData() {
  loading.value = true
  try {
    if (!stockInfo.value) {
      try {
        const infoRes = await stockApi.get(stockId.value)
        stockInfo.value = infoRes.data
        if (stockInfo.value) code.value = stockInfo.value.code
      } catch (e) {
        console.error('加载股票信息失败', e)
      }
    }

    const dailyRes = await stockApi.getDaily(stockId.value, { days: days.value })
    dailyData.value = dailyRes.data.results || dailyRes.data
    const latestRes = await stockApi.getDailyLatest(stockId.value)
    latestQuote.value = latestRes.data

    // 默认加载当日走势
    if (latestQuote.value?.trade_date) {
      await showIntraday(latestQuote.value.trade_date)
    } else {
      minuteData.value = []
      intradayDate.value = ''
      intradayQuote.value = null
    }
  } catch (e) {
    console.error('加载股票数据失败', e)
  } finally {
    loading.value = false
  }
}

async function showIntraday(date) {
  intradayDate.value = date
  // 从 dailyData 中找该日的行情数据
  const dayQuote = dailyData.value.find(d => d.trade_date === date)
  intradayQuote.value = dayQuote || latestQuote.value
  await loadMinuteData(date)
}

async function loadMinuteData(date) {
  try {
    const res = await stockApi.getMinutes(stockId.value, { date })
    minuteData.value = res.data.results || res.data
  } catch (e) {
    console.error('加载分钟数据失败', e)
    minuteData.value = []
  }
}

// K线图点击某日时切换走势图
function onKlineDateClick(date) {
  showIntraday(date)
  // 滚动到走势图区域
  document.querySelector('.intraday-section')?.scrollIntoView({ behavior: 'smooth' })
}

function resetAndLoad() {
  stockInfo.value = null
  code.value = ''
  dailyData.value = []
  latestQuote.value = null
  minuteData.value = []
  intradayDate.value = ''
  intradayQuote.value = null
  loadData()
}

onMounted(() => {
  loadData()
})

watch(stockId, () => {
  resetAndLoad()
})

// 两融依赖 code：code 在 loadData 拉到股票信息后才赋值，
// 因此监听 code 变化再加载（首次进入和切换股票都会触发）
watch(code, (c) => {
  if (c) loadMargin()
  else margin.value = null
})
</script>

<template>
  <div class="stock-detail" v-if="!loading || latestQuote">
    <div class="detail-header">
      <router-link to="/" class="back-link">← 返回面板</router-link>
      <h1 v-if="latestQuote">
        {{ latestQuote.code }} {{ latestQuote.name }}
      </h1>
      <h1 v-else>{{ code }}</h1>
    </div>

    <!-- 数据摘要 -->
    <div class="quote-summary" v-if="latestQuote">
      <div class="summary-grid">
        <div class="summary-item">
          <span class="label">交易日期</span>
          <span class="value">{{ latestQuote.trade_date }}</span>
        </div>
        <div class="summary-item">
          <span class="label">开盘价</span>
          <span class="value">{{ formatNum(latestQuote.open_price) }}</span>
        </div>
        <div class="summary-item">
          <span class="label">收盘价</span>
          <span class="value">{{ formatNum(latestQuote.close_price) }}</span>
        </div>
        <div class="summary-item">
          <span class="label">涨跌幅</span>
          <span class="value" :class="pctClass(latestQuote.open_close_pct)">
            {{ formatPct(latestQuote.open_close_pct) }}
          </span>
        </div>
        <div class="summary-item">
          <span class="label">涨跌价</span>
          <span class="value" :class="pctClass(latestQuote.open_close_diff)">
            {{ formatNum(latestQuote.open_close_diff) }}
          </span>
        </div>
        <div class="summary-item">
          <span class="label">振幅</span>
          <span class="value">{{ formatPct(latestQuote.high_low_pct) }}</span>
        </div>
        <div class="summary-item highlight">
          <span class="label">最高价</span>
          <span class="value up">{{ formatNum(latestQuote.high_price) }}</span>
        </div>
        <div class="summary-item highlight">
          <span class="label">最高点时间</span>
          <span class="value up">{{ formatDateTime(latestQuote.high_time) }}</span>
        </div>
        <div class="summary-item highlight">
          <span class="label">最低价</span>
          <span class="value down">{{ formatNum(latestQuote.low_price) }}</span>
        </div>
        <div class="summary-item highlight">
          <span class="label">最低点时间</span>
          <span class="value down">{{ formatDateTime(latestQuote.low_time) }}</span>
        </div>
      </div>
    </div>

    <div v-else class="no-data">暂无数据，请先在面板页点击"刷新数据"</div>

    <!-- 当日走势图 -->
    <div class="intraday-section">
      <IntradayChart
        :minuteData="minuteData"
        :dailyQuote="intradayQuote"
        :date="intradayDate"
      />
    </div>

    <!-- K线图 -->
    <div class="chart-section">
      <h2>日K线图</h2>
      <div class="days-select">
        <label>显示天数：</label>
        <select v-model="days" @change="loadData">
          <option :value="7">7天</option>
          <option :value="30">30天</option>
          <option :value="90">90天</option>
          <option :value="180">180天</option>
          <option :value="365">1年</option>
        </select>
      </div>
      <KlineChart
        :data="dailyData"
        :code="code || stockId"
        @date-click="onKlineDateClick"
      />
    </div>

    <!-- 两融余额（交易所官方披露） -->
    <div class="chart-section margin-section" v-if="margin">
      <h2>两融余额（融资融券）</h2>
      <template v-if="margin.available">
        <div class="margin-grid">
          <div class="margin-item">
            <span class="label">融资余额</span>
            <span class="value">{{ margin.rz != null ? margin.rz.toLocaleString() + ' 亿' : '-' }}</span>
          </div>
          <div class="margin-item" v-if="margin.rq != null">
            <span class="label">融券余额</span>
            <span class="value">{{ margin.rq.toLocaleString() }} 亿</span>
          </div>
          <div class="margin-item">
            <span class="label">两融余额</span>
            <span class="value">{{ margin.total != null ? margin.total.toLocaleString() + ' 亿' : '-' }}</span>
          </div>
          <div class="margin-item">
            <span class="label">融资余额日变化</span>
            <span class="value" :class="pctClass(margin.chg_1d)">
              {{ margin.chg_1d != null ? (margin.chg_1d > 0 ? '+' : '') + margin.chg_1d + ' 亿' : '-' }}
            </span>
          </div>
        </div>
        <div class="margin-note">
          截至 {{ margin.date }} · 交易所 T+1 披露<span v-if="margin.message"> · {{ margin.message }}</span>
        </div>
      </template>
      <div v-else class="margin-note">{{ margin.message || '暂不可用' }}</div>
    </div>

    <!-- AI 分析 -->
    <AiAnalysisPanel :stockId="stockId" />
  </div>

  <div v-else class="loading">加载中...</div>
</template>

<style scoped>
.detail-header {
  margin-bottom: 24px;
}

.back-link {
  color: #e94560;
  text-decoration: none;
  font-size: 14px;
}

.back-link:hover {
  text-decoration: underline;
}

.detail-header h1 {
  color: #eee;
  font-size: 28px;
  margin-top: 8px;
}

.quote-summary {
  background: #16213e;
  border-radius: 8px;
  padding: 20px;
  margin-bottom: 24px;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 16px;
}

.summary-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.summary-item.highlight {
  background: #1a2744;
  padding: 12px;
  border-radius: 6px;
}

.summary-item .label {
  color: #888;
  font-size: 12px;
}

.summary-item .value {
  color: #ddd;
  font-size: 18px;
  font-weight: bold;
}

.up {
  color: #e94560;
}

.down {
  color: #00c853;
}

.no-data {
  text-align: center;
  color: #666;
  padding: 60px;
  background: #16213e;
  border-radius: 8px;
  margin-bottom: 24px;
}

.intraday-section {
  margin-bottom: 24px;
}

.chart-section {
  background: #16213e;
  border-radius: 8px;
  padding: 20px;
}

.chart-section h2 {
  color: #eee;
  font-size: 18px;
  margin-bottom: 12px;
}

.days-select {
  margin-bottom: 16px;
  color: #aaa;
}

.days-select select {
  background: #1a1a2e;
  color: #ddd;
  border: 1px solid #333;
  padding: 4px 8px;
  border-radius: 4px;
  margin-left: 8px;
}

.loading {
  text-align: center;
  padding: 60px;
  color: #888;
}

@media (max-width: 768px) {
  .summary-grid {
    grid-template-columns: repeat(2, 1fr);
  }

  .detail-header h1 {
    font-size: 22px;
  }
}
.margin-section { margin-top: 16px; }
.margin-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 10px;
}
.margin-item {
  background: #1a1a2e;
  border-radius: 8px;
  padding: 10px 12px;
}
.margin-item .label { display: block; color: #888; font-size: 12px; margin-bottom: 4px; }
.margin-item .value { font-size: 16px; font-weight: bold; color: #eee; }
.margin-note { margin-top: 8px; font-size: 12px; color: #888; }
</style>
