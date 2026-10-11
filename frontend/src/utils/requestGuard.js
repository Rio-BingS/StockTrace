/**
 * 请求竞态守卫（FEF-06），不依赖 Vue，便于 node:test 单测。
 * 每次 next() 取消上一次未完成的请求，并发一个序号；
 * 响应回来时用 isLatest() 判断是否仍是最新一次，过期响应直接丢弃，
 * 避免晚到的旧响应覆盖新状态。abort() 用于组件卸载时取消在途请求。
 */
export function createRequestGuard() {
  let seq = 0
  let controller = null

  function next() {
    if (controller) controller.abort()
    controller = new AbortController()
    const mySeq = ++seq
    return { signal: controller.signal, isLatest: () => mySeq === seq }
  }

  function abort() {
    seq += 1
    if (controller) controller.abort()
    controller = null
  }

  return { next, abort }
}
