/**
 * 浏览器空闲时预取（FEX-07）：首页不含图表，进入详情/市场页时才下载 ECharts（~480KB），
 * 首访点进去会卡一下。首页渲染完、浏览器空闲时先把图表库拉进缓存。
 * 省流量模式（Save-Data）或 2G 网络下跳过。
 */
export function shouldPrefetch(nav = globalThis.navigator) {
  const c = nav?.connection
  if (!c) return true
  if (c.saveData) return false
  return !/2g/.test(c.effectiveType || '')
}

export function prefetchWhenIdle(
  loaders,
  { win = globalThis.window, nav = globalThis.navigator, timeout = 3000 } = {},
) {
  if (!win || !shouldPrefetch(nav)) return false
  const run = () =>
    loaders.forEach((load) =>
      load().catch(() => {
        /* 预取失败不影响正常按需加载 */
      }),
    )
  if (typeof win.requestIdleCallback === 'function') win.requestIdleCallback(run, { timeout })
  else win.setTimeout(run, timeout)
  return true
}
