import { onActivated, onDeactivated, onMounted, onUnmounted } from 'vue'
import { createPoller } from '../utils/poller.js'

/**
 * 组件内轮询（FEF-04）：挂载/激活时启动，失活/卸载时停止，页面隐藏时暂停，
 * 非交易时段降到 10 分钟一次；KeepAlive 返回时数据超过 5 分钟静默刷新。
 * fn 首次由本函数调用（无需在 onMounted 里再调）。
 * 返回 { refresh }：手动刷新并重新计时。
 */
export function usePolling(fn, options = {}) {
  const poller = createPoller(fn, options)
  const onVisibility = () => poller.onVisibilityChange()

  onMounted(() => {
    document.addEventListener('visibilitychange', onVisibility)
    poller.start()
  })
  // KeepAlive 缓存的页面：首次挂载时 onActivated 也会触发，start 内部去重
  onActivated(() => poller.start())
  onDeactivated(() => poller.stop())
  onUnmounted(() => {
    document.removeEventListener('visibilitychange', onVisibility)
    poller.stop()
  })

  return { refresh: poller.refresh }
}
