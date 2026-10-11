import { createRouter, createWebHistory } from 'vue-router'
import Dashboard from '../views/Dashboard.vue'
import { pageTitle, scrollBehavior } from '../utils/routerHelpers.js'

// 首页静态导入；其余页面按路由懒加载，避免 echarts 与市场页全部打进主包
const StockDetail = () => import('../views/StockDetail.vue')
const Market = () => import('../views/Market.vue')
const MarketTrend = () => import('../views/MarketTrend.vue')
const MarketSectors = () => import('../views/MarketSectors.vue')
const MarketInstitutions = () => import('../views/MarketInstitutions.vue')
const MarketNationalEtf = () => import('../views/MarketNationalEtf.vue')
const MarketEtfRadar = () => import('../views/MarketEtfRadar.vue')
const Detector = () => import('../views/Detector.vue')
const Settings = () => import('../views/Settings.vue')
const Screener = () => import('../views/Screener.vue')
const NotFound = () => import('../views/NotFound.vue')

const routes = [
  {
    path: '/',
    name: 'Dashboard',
    component: Dashboard,
    meta: { title: '自选' },
  },
  {
    path: '/market',
    name: 'Market',
    component: Market,
    meta: { title: '市场概览', keepAlive: true },
  },
  {
    path: '/market/trend',
    name: 'MarketTrend',
    component: MarketTrend,
    meta: { title: '指数走势', keepAlive: true },
  },
  {
    path: '/market/sectors',
    name: 'MarketSectors',
    component: MarketSectors,
    meta: { title: '板块资金', keepAlive: true },
  },
  {
    path: '/market/institutions',
    name: 'MarketInstitutions',
    component: MarketInstitutions,
    meta: { title: '机构动向', keepAlive: true },
  },
  {
    path: '/market/national-etf',
    name: 'MarketNationalEtf',
    component: MarketNationalEtf,
    meta: { title: '国家队 ETF', keepAlive: true },
  },
  {
    path: '/market/etf-radar',
    name: 'MarketEtfRadar',
    component: MarketEtfRadar,
    meta: { title: 'ETF 雷达', keepAlive: true },
  },
  {
    path: '/detector',
    name: 'Detector',
    component: Detector,
    meta: { title: '探测器' },
  },
  {
    // 只匹配数字 id，/stock/abc 落到 404 而不是发出必然失败的请求
    path: '/stock/:id(\\d+)',
    name: 'StockDetail',
    component: StockDetail,
    meta: { title: '个股详情' },
  },
  {
    path: '/screener',
    name: 'Screener',
    component: Screener,
    meta: { title: '条件选股' },
  },
  {
    path: '/settings',
    name: 'Settings',
    component: Settings,
    meta: { title: '设置' },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'NotFound',
    component: NotFound,
    meta: { title: '页面不存在' },
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior,
})

router.afterEach((to) => {
  document.title = pageTitle(to.meta.title)
})

export default router
