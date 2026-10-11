/**
 * 按字符串字段升序排序的比较函数（FEF-17）。
 * 接口返回的日期/时间是定长 ISO 文本（"2026-10-09"、"2026-10-09 09:31:00"），
 * 直接比较字符串即可；new Date("2026-10-09 09:31:00") 在 Safari 上是 Invalid Date，
 * 排序会整体失效。
 */
export function byKey(key) {
  return (a, b) => {
    const x = String(a?.[key] ?? '')
    const y = String(b?.[key] ?? '')
    return x < y ? -1 : x > y ? 1 : 0
  }
}
