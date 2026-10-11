// AI 后台任务轮询（P1 遗留：AI 调用异步化）
// 后端 ?async=1 时返回 202 + job_id，这里按间隔轮询 /api/ai/jobs/<id>/ 直到 done/failed。

function canceledError() {
  const err = new Error('canceled')
  err.name = 'CanceledError'
  err.code = 'ERR_CANCELED'
  return err
}

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(canceledError())
    const t = setTimeout(() => {
      signal?.removeEventListener?.('abort', onAbort)
      resolve()
    }, ms)
    function onAbort() {
      clearTimeout(t)
      reject(canceledError())
    }
    signal?.addEventListener?.('abort', onAbort, { once: true })
  })
}

/**
 * 轮询任务直到结束。
 *   fetchJob(): Promise<{ data: { status, queued_ahead?, http_status?, result? } }>
 *   onStatus(job)：每次拿到状态时回调（queued/running），用于界面显示"已排队/分析中"
 * 成功返回 result；失败抛出与 axios 错误同形的对象（err.response.status/data），
 * 可直接交给 aiErrorText；signal 中止时抛 CanceledError；超过 timeoutMs 抛超时错误。
 */
export async function pollAiJob(
  fetchJob,
  { signal, intervalMs = 2000, timeoutMs = 180000, onStatus } = {},
) {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    if (signal?.aborted) throw canceledError()
    const res = await fetchJob()
    const job = res?.data || {}
    if (job.status === 'done') return job.result || {}
    if (job.status === 'failed') {
      const err = new Error('AI 任务失败')
      err.response = { status: job.http_status || 500, data: job.result || {} }
      throw err
    }
    onStatus?.(job)
    if (Date.now() + intervalMs > deadline) {
      const err = new Error('timeout')
      err.code = 'ECONNABORTED'
      throw err
    }
    await sleep(intervalMs, signal)
  }
}

// 202 + job_id 才是异步任务；旧后端直接 200 返回结果时调用方按同步处理
export function isQueuedJob(res) {
  return res?.status === 202 && typeof res?.data?.job_id === 'string'
}
