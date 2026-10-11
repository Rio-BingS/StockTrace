/**
 * 分时价格序列的阶段高低点检测（ARC-09：从 IntradayChart 抽出，便于单测）。
 * 某点在左右各 windowSize 根内均为最高（最低）即为峰（谷）；
 * 与上一个峰（谷）的价差小于全天振幅 1% 的视为噪声丢弃。
 */
export function findLocalExtrema(prices, windowSize = 20) {
  if (prices.length < windowSize * 2 + 1) return { peaks: [], valleys: [] }

  const peaks = []
  const valleys = []
  const fullRange = Math.max(...prices) - Math.min(...prices)
  const minSwing = fullRange * 0.01 // 至少为全天振幅的1%

  for (let i = windowSize; i < prices.length - windowSize; i++) {
    const leftSlice = prices.slice(i - windowSize, i)
    const rightSlice = prices.slice(i + 1, i + windowSize + 1)
    const current = prices[i]

    const isPeak = leftSlice.every(v => v <= current) && rightSlice.every(v => v <= current)
    const isValley = leftSlice.every(v => v >= current) && rightSlice.every(v => v >= current)

    if (isPeak) {
      // 与上一个峰/谷比较，过滤过小幅波动
      const lastPeak = peaks.length > 0 ? peaks[peaks.length - 1] : null
      if (!lastPeak || Math.abs(current - lastPeak.price) >= minSwing) {
        peaks.push({ index: i, price: current })
      }
    }
    if (isValley) {
      const lastValley = valleys.length > 0 ? valleys[valleys.length - 1] : null
      if (!lastValley || Math.abs(current - lastValley.price) >= minSwing) {
        valleys.push({ index: i, price: current })
      }
    }
  }

  return { peaks, valleys }
}
