/**
 * 轮询调度核心（FEF-04），不依赖 Vue，时钟/可见性可注入，便于 node:test 单测。
 * - 串行：上一次 fn 结束后才排下一次，不会叠加请求
 * - 交易时段按 interval，非交易时段降到 idleInterval（见 tradingTime.pollDelay）
 * - 页面隐藏时不排定时器；恢复可见时若已超过一个间隔则立即补一次
 * - stop 后再 start（KeepAlive 返回）：距上次加载超过 staleAfter 才静默刷新
 */
import { pollDelay } from './tradingTime.js'

const defaultHidden = () => typeof document !== 'undefined' && document.visibilityState === 'hidden'

export function createPoller(
  fn,
  {
    interval,
    idleInterval = 10 * 60 * 1000,
    tradingOnly = true,
    staleAfter = 5 * 60 * 1000,
    now = () => Date.now(),
    isHidden = defaultHidden,
  } = {},
) {
  let active = false
  let timer = null
  let inFlight = null
  let lastRun = 0

  function clear() {
    if (timer) {
      clearTimeout(timer)
      timer = null
    }
  }

  function currentDelay() {
    return pollDelay(new Date(now()), { interval, idleInterval, tradingOnly })
  }

  function schedule() {
    clear()
    if (!active || isHidden()) return
    timer = setTimeout(tick, currentDelay())
  }

  // 同一时刻只跑一个 fn；并发调用复用同一个 Promise
  function run() {
    if (inFlight) return inFlight
    inFlight = Promise.resolve()
      .then(fn)
      .catch(() => {}) // fn 自己负责展示错误，这里只保证调度不中断
      .finally(() => {
        lastRun = now()
        inFlight = null
      })
    return inFlight
  }

  function tick() {
    timer = null
    return run().then(schedule)
  }

  /** 启动；首次启动或数据已过期时立即跑一次 */
  function start() {
    if (active) return
    active = true
    if (!lastRun || now() - lastRun >= staleAfter) {
      clear()
      run().then(schedule)
    } else {
      schedule()
    }
  }

  function stop() {
    active = false
    clear()
  }

  /** 手动刷新：立即跑一次并从此刻重新计时 */
  function refresh() {
    clear()
    return run().then(schedule)
  }

  function onVisibilityChange() {
    if (!active) return
    if (isHidden()) {
      clear()
    } else if (!lastRun || now() - lastRun >= currentDelay()) {
      clear()
      run().then(schedule)
    } else {
      schedule()
    }
  }

  return {
    start,
    stop,
    refresh,
    onVisibilityChange,
    isActive: () => active,
    hasTimer: () => timer !== null,
    lastRun: () => lastRun,
  }
}
