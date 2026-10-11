import { onBeforeUnmount, ref } from 'vue'

/** 响应式媒体查询（FEX-05）：如 useMediaQuery('(max-width: 640px)') 判断窄屏 */
export function useMediaQuery(query) {
  const mql = typeof window !== 'undefined' && window.matchMedia ? window.matchMedia(query) : null
  const matches = ref(mql ? mql.matches : false)
  if (mql) {
    const onChange = (e) => { matches.value = e.matches }
    mql.addEventListener('change', onChange)
    onBeforeUnmount(() => mql.removeEventListener('change', onChange))
  }
  return matches
}
