/** 路由层的纯函数：页面标题与滚动行为（FEF-16） */

export const SITE_NAME = 'StockTrace'

/** 页面标题："自选 · StockTrace"；没有 meta.title 时只用站点名 */
export function pageTitle(title) {
  return title ? `${title} · ${SITE_NAME}` : `${SITE_NAME} - A股监控`
}

/**
 * 滚动行为：浏览器前进/后退恢复原位置；带锚点时滚到锚点；
 * 只改 query（筛选、翻页、选中 ETF）的同页跳转保持当前位置；其它跳转回到顶部。
 */
export function scrollBehavior(to, from, savedPosition) {
  if (savedPosition) return savedPosition
  if (to.hash) return { el: to.hash }
  if (from && to.path === from.path) return false
  return { top: 0 }
}
