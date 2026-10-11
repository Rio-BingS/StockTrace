import { watch } from 'vue'
import { useRoute } from 'vue-router'
import { assignRefs, changedKeys } from '../utils/routeQuery.js'

/**
 * KeepAlive 页面响应 URL query 变化（FEF-15）。
 * - 只在当前路由仍是本页时处理（切到别的页后 query 变化与本页无关）
 * - 自己 router.replace 写回的 query 与当前状态一致，不会重复加载
 * - onChange(keys)：有键变化时调用，由页面决定是否重新请求
 */
export function useRouteQuery(routeName, refs, parse, onChange) {
  const route = useRoute()
  watch(() => route.query, (query) => {
    if (route.name !== routeName) return
    const next = parse(query)
    const keys = changedKeys(refs, next)
    if (!keys.length) return
    assignRefs(refs, next)
    onChange(keys)
  })
}
