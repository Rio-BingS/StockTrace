import { getCurrentInstance, onUnmounted, ref } from 'vue'
import { createRequestGuard } from '../utils/requestGuard.js'
import { errorMessage, isCanceled } from '../utils/errorMessage.js'

/**
 * 异步加载样板（ARC-06）：loading / error / data 三件套 + 竞态守卫（FEF-06 的 createRequestGuard）。
 * - fetcher({ signal }, ...args) 返回要放进 data 的值
 * - 新一次 run 会取消上一次；过期或主动取消的响应不写 data、不报错、不改 loading
 * - 在组件 setup 中调用时，卸载自动取消进行中的请求
 */
export function useAsync(fetcher, { errorText = '加载失败', initial = null, onError } = {}) {
  const data = ref(initial)
  const loading = ref(false)
  const error = ref('')
  const guard = createRequestGuard()

  async function run(...args) {
    const req = guard.next()
    loading.value = true
    error.value = ''
    try {
      const value = await fetcher({ signal: req.signal }, ...args)
      if (req.isLatest()) data.value = value
      return value
    } catch (e) {
      if (req.isLatest() && !isCanceled(e)) {
        error.value = errorMessage(e, errorText)
        onError?.(e)
      }
      return undefined
    } finally {
      if (req.isLatest()) loading.value = false
    }
  }

  if (getCurrentInstance()) onUnmounted(() => guard.abort())

  return { data, loading, error, run, abort: () => guard.abort() }
}
