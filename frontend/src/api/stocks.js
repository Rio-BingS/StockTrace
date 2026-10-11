import axios from 'axios'
import { errorMessage, isCanceled } from '../utils/errorMessage.js'

const api = axios.create({
  baseURL: '/api',
  timeout: 15000,
  // 后端据此区分前端 XHR 与跨站表单提交（SEC-03），勿删
  headers: { 'X-Requested-With': 'XMLHttpRequest' },
})

// 统一错误文案（FEF-07）：给每个失败请求挂上 err.userMessage，
// 区分 DRF detail/校验对象、nginx HTML 网关页、超时、429 等，
// 避免页面显示 [object Object] 或整页 HTML。主动取消的请求不挂文案。
api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err && !isCanceled(err)) err.userMessage = errorMessage(err)
    return Promise.reject(err)
  },
)

// 股票相关 API
// 读接口最后一个参数可传 { signal }（AbortController），用于取消过期请求（FEF-06）
export const stockApi = {
  // 获取关注列表
  getList({ signal } = {}) {
    return api.get('/stocks/', { signal })
  },

  // 单只关注详情
  get(id, { signal } = {}) {
    return api.get(`/stocks/${id}/`, { signal })
  },

  // 添加关注
  add(code, name = '') {
    return api.post('/stocks/', { code, name })
  },

  // 搜索股票（代码或名称）
  search(keyword, { signal } = {}) {
    return api.get('/stocks/search/', { params: { q: keyword }, signal })
  },

  // 取消关注
  remove(id) {
    return api.delete(`/stocks/${id}/`)
  },

  // 更新关注项（持仓成本价 / 持股数等）
  update(id, data) {
    return api.patch(`/stocks/${id}/`, data)
  },

  // 获取日K数据
  getDaily(id, params = {}, { signal } = {}) {
    return api.get(`/stocks/${id}/daily/`, { params, signal })
  },

  // 获取最新一天行情
  getDailyLatest(id, { signal } = {}) {
    return api.get(`/stocks/${id}/daily/latest/`, { signal })
  },

  // 获取分钟K线
  getMinutes(id, params = {}, { signal } = {}) {
    return api.get(`/stocks/${id}/minutes/`, { params, signal })
  },

  // 手动拉取单只股票数据（后台任务，立即返回）
  fetchStock(id) {
    return api.post(`/stocks/${id}/fetch/`)
  },

  // 手动拉取所有股票数据（后台任务，立即返回）
  fetchAll() {
    return api.post('/stocks/fetch-all/')
  },

  // 后台拉取任务状态
  fetchStatus({ signal } = {}) {
    return api.get('/stocks/fetch-status/', { signal })
  },
}

// Dashboard
export const dashboardApi = {
  get({ signal } = {}) {
    return api.get('/dashboard/', { signal })
  },
}

// 市场数据（指数 / 资金 / ETF 等，外部源可能较慢）
const marketTimeout = { timeout: 60000 }

export const marketApi = {
  getOverview({ signal } = {}) {
    return api.get('/market/', { ...marketTimeout, signal })
  },
  getTrend(params = {}, { signal } = {}) {
    return api.get('/market/trend/', { ...marketTimeout, params, signal })
  },
  getSectors(params = {}, { signal } = {}) {
    return api.get('/market/sectors/', { ...marketTimeout, params, signal })
  },
  getNationalEtf({ signal } = {}) {
    return api.get('/market/national-etf/', { ...marketTimeout, signal })
  },
  // 区间资金流向需逐只拉取 18 只 ETF 的历史资金流，冷启动较慢；
  // params 传 { period } 或 { start, end }（自定义区间）
  getNationalEtfFlow(params = {}, { signal } = {}) {
    return api.get('/market/national-etf/flow/', { timeout: 110000, params, signal })
  },
  getEtfRadar(params = {}, { signal } = {}) {
    return api.get('/market/etf-radar/', { ...marketTimeout, params, signal })
  },
  getEtfDetail(code, params = {}, { signal } = {}) {
    return api.get(`/market/etfs/${code}/`, { ...marketTimeout, params, signal })
  },
  // 个股两融余额（交易所官方披露，T+1；非两融标的如实降级）
  getStockMargin(code, { signal } = {}) {
    return api.get(`/market/stock-margin/${code}/`, { ...marketTimeout, signal })
  },
  // 大盘主力资金流区间聚合（上游仅约 120 个交易日，超出会标 truncated）
  getMarketFlow(params = {}, { signal } = {}) {
    return api.get('/market/market-flow/', { ...marketTimeout, params, signal })
  },
  // 北向资金净买额区间聚合
  getNorthbound(params = {}, { signal } = {}) {
    return api.get('/market/northbound/', { ...marketTimeout, params, signal })
  },
  getInstitutions(code, { signal } = {}) {
    const params = code ? { code } : {}
    return api.get('/market/institutions/', { ...marketTimeout, params, signal })
  },
}

// 探测器（平静盘面下的渐进微变监测，日度扫描结果）
export const detectorApi = {
  // 信号列表；params 传 { severity, direction, target_kind, status, date, all_dates, page, page_size }
  getSignals(params = {}, { signal } = {}) {
    return api.get('/detector/signals/', { ...marketTimeout, params, signal })
  },
  // 信号详情（含各微变维度近 20 日曲线）
  getSignalDetail(id, { signal } = {}) {
    return api.get(`/detector/signals/${id}/`, { ...marketTimeout, signal })
  },
  // 板块微变热力图；params 传 { board: 'industry' | 'concept' }
  getHeatmap(params = {}, { signal } = {}) {
    return api.get('/detector/heatmap/', { ...marketTimeout, params, signal })
  },
  // 维度配置与迭代状态（权重/阈值/停用原因/近期运行摘要）
  getConfig({ signal } = {}) {
    return api.get('/detector/config/', { ...marketTimeout, signal })
  },
}

// 自选分组
export const groupApi = {
  list({ signal } = {}) {
    return api.get('/stock-groups/', { signal })
  },
  create(name) {
    return api.post('/stock-groups/', { name })
  },
  update(id, data) {
    return api.patch(`/stock-groups/${id}/`, data)
  },
  remove(id) {
    return api.delete(`/stock-groups/${id}/`)
  },
}

// 价格提醒
export const alertApi = {
  // 规则列表（含未读提醒数）
  list({ signal } = {}) {
    return api.get('/alerts/', { signal })
  },
  create(data) {
    return api.post('/alerts/', data)
  },
  update(id, data) {
    return api.patch(`/alerts/${id}/`, data)
  },
  remove(id) {
    return api.delete(`/alerts/${id}/`)
  },
  // 触发记录；params 传 { unread: 1 } 只看未读
  events(params = {}, { signal } = {}) {
    return api.get('/alerts/events/', { params, signal })
  },
  // 标记已读；data 传 { ids: [...] } 或 {} 全部已读
  markRead(data = {}) {
    return api.post('/alerts/events/read/', data)
  },
}

// 条件选股预设
export const presetApi = {
  list({ signal } = {}) {
    return api.get('/screener/presets/', { signal })
  },
  create(name, spec) {
    return api.post('/screener/presets/', { name, spec })
  },
  remove(id) {
    return api.delete(`/screener/presets/${id}/`)
  },
}

// AI 功能（LLM 调用较慢，单独放宽超时）；可传 { signal } 取消进行中的请求（AIX-05）
const aiTimeout = { timeout: 120000 }

export const aiApi = {
  // AI 服务商配置 CRUD
  listProviders() {
    return api.get('/ai-providers/')
  },
  createProvider(data) {
    return api.post('/ai-providers/', data)
  },
  updateProvider(id, data) {
    return api.patch(`/ai-providers/${id}/`, data)
  },
  deleteProvider(id) {
    return api.delete(`/ai-providers/${id}/`)
  },
  testProvider(id) {
    return api.post(`/ai-providers/${id}/test/`, {}, { timeout: 30000 })
  },

  // 个股 AI 分析（按需触发）
  analyzeStock(id, { signal } = {}) {
    return api.post(`/stocks/${id}/ai-analysis/`, {}, { ...aiTimeout, signal })
  },

  // 个股 AI 分析（后台排队）：返回 202 + job_id，再用 aiJob 轮询结果；请求本身很快
  analyzeStockAsync(id, { signal } = {}) {
    return api.post(`/stocks/${id}/ai-analysis/?async=1`, {}, { timeout: 30000, signal })
  },

  // 查询 AI 后台任务状态与结果
  aiJob(jobId, { signal } = {}) {
    return api.get(`/ai/jobs/${encodeURIComponent(jobId)}/`, { timeout: 30000, signal })
  },

  // 结构化条件选股（不调 LLM，快速）
  screener(spec) {
    return api.post('/screener/', spec, { timeout: 30000 })
  },

  // 自然语言选股：LLM 翻译成条件后执行
  screenerAi(query, { signal } = {}) {
    return api.post('/screener/ai/', { query }, { ...aiTimeout, signal })
  },

  // 对筛选结果做 AI 点评
  screenerComment(query, results, { signal } = {}) {
    return api.post('/screener/ai/comment/', { query, results }, { ...aiTimeout, signal })
  },

  // 自然语言选股 / AI 点评（后台排队）：返回 202 + job_id，再用 aiJob 轮询结果
  screenerAiAsync(query, { signal } = {}) {
    return api.post('/screener/ai/?async=1', { query }, { timeout: 30000, signal })
  },
  screenerCommentAsync(query, results, { signal } = {}) {
    return api.post('/screener/ai/comment/?async=1', { query, results }, { timeout: 30000, signal })
  },
}

export default api
