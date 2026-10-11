/**
 * 串行合并调度（FEF-05），不依赖 Vue，便于 node:test 单测。
 */

/**
 * 把异步函数包成"串行 + 合并"版本：
 * - 空闲时调用立即执行
 * - 执行中再次调用（不论多少次）只在本次结束后补跑一次，且共用同一个 Promise
 * 这样轮询、手动刷新、添加后补刷同时触发时，后端最多只有一个请求在跑。
 */
export function createSerialRunner(fn) {
  let running = null
  let pending = null

  function start() {
    let p
    try {
      p = Promise.resolve(fn()) // 同步调用，保证 loading 等状态立即生效
    } catch (e) {
      p = Promise.reject(e)
    }
    running = p.finally(() => {
      running = null
    })
    return running
  }

  return function run() {
    if (!running) return start()
    if (!pending) {
      // 当前这次结束后（无论成败）补跑一次
      pending = running
        .catch(() => {})
        .then(() => {
          pending = null
          return start()
        })
    }
    return pending
  }
}

/**
 * 按固定延迟序列补刷（如添加自选后 3/8/15s 各刷一次）：
 * 只持有一个定时器句柄，重复 trigger 会从头重新排，不叠加；cancel 用于卸载清理。
 */
export function createBurstScheduler(run, delays = [3000, 8000, 15000]) {
  let timer = null
  let gen = 0

  function cancel() {
    gen += 1
    if (timer) {
      clearTimeout(timer)
      timer = null
    }
  }

  function trigger() {
    cancel()
    const myGen = gen
    let elapsed = 0
    let idx = 0
    const next = () => {
      if (idx >= delays.length) {
        timer = null
        return
      }
      const wait = delays[idx] - elapsed
      elapsed = delays[idx]
      idx += 1
      timer = setTimeout(
        async () => {
          timer = null
          try {
            await run()
          } catch {
            // run 自己负责展示错误
          }
          if (myGen === gen) next()
        },
        Math.max(0, wait),
      )
    }
    next()
  }

  return { trigger, cancel, isScheduled: () => timer !== null }
}
