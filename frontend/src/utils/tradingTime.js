/**
 * A 股交易时段判断（FEF-04），纯函数，便于 node:test 单测。
 * 交易时段按北京时间（UTC+8，无夏令时）：周一至周五 9:15–11:30、13:00–15:00，
 * 9:15 起含集合竞价。不识别法定节假日（节假日按非交易日对待需后端日历，
 * 这里只用于降低轮询频率，误判的代价只是多轮询几次）。
 */

const BEIJING_OFFSET_MIN = 8 * 60

// [开始, 结束] 分钟数（含两端）
const SESSIONS = [
  [9 * 60 + 15, 11 * 60 + 30],
  [13 * 60, 15 * 60],
]

/** 把任意时刻换算成北京时间的 { weekday(0=周日), minutes(当日分钟数) } */
export function beijingClock(date = new Date()) {
  const shifted = new Date(date.getTime() + BEIJING_OFFSET_MIN * 60000)
  return {
    weekday: shifted.getUTCDay(),
    minutes: shifted.getUTCHours() * 60 + shifted.getUTCMinutes(),
  }
}

/** 是否处于 A 股交易时段（北京时间，与运行环境时区无关） */
export function isTradingTime(date = new Date()) {
  const { weekday, minutes } = beijingClock(date)
  if (weekday === 0 || weekday === 6) return false
  return SESSIONS.some(([start, end]) => minutes >= start && minutes <= end)
}

/**
 * 计算下一次轮询间隔：交易时段用 interval；非交易时段用 idleInterval（默认 10 分钟）。
 * tradingOnly=false 时始终用 interval。
 */
export function pollDelay(date, { interval, idleInterval = 10 * 60 * 1000, tradingOnly = true }) {
  if (!tradingOnly || isTradingTime(date)) return interval
  return Math.max(interval, idleInterval)
}
