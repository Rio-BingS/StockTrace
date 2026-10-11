import { createRouter, createWebHistory } from 'vue-router'
import Dashboard from '../views/Dashboard.vue'

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

const routes = [
  {
    path: '/',
    name: 'Dashboard',
    component: Dashboard,
  },
  {
    path: '/market',
    name: 'Market',
    component: Market,
    meta: { keepAlive: true },
  },
  {
    path: '/market/trend',
    name: 'MarketTrend',
    component: MarketTrend,
    meta: { keepAlive: true },
  },
  {
    path: '/market/sectors',
    name: 'MarketSectors',
    component: MarketSectors,
    meta: { keepAlive: true },
  },
  {
    path: '/market/institutions',
    name: 'MarketInstitutions',
    component: MarketInstitutions,
    meta: { keepAlive: true },
  },
  {
    path: '/market/national-etf',
    name: 'MarketNationalEtf',
    component: MarketNationalEtf,
    meta: { keepAlive: true },
  },
  {
    path: '/market/etf-radar',
    name: 'MarketEtfRadar',
    component: MarketEtfRadar,
    meta: { keepAlive: true },
  },
  {
    path: '/detector',
    name: 'Detector',
    component: Detector,
  },
  {
    path: '/stock/:id',
    name: 'StockDetail',
    component: StockDetail,
    props: true,
  },
  {
    path: '/screener',
    name: 'Screener',
    component: Screener,
  },
  {
    path: '/settings',
    name: 'Settings',
    component: Settings,
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
