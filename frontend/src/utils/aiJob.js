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

/**
 * 提交 AI 请求并在需要时轮询后台任务，统一返回结果数据（P3：选股翻译/点评改走后台任务）。
 *   submit(): 发起请求（如 aiApi.screenerAiAsync），返回 axios 响应
 *   fetchJob(jobId): 查询任务（如 aiApi.aiJob）
 * 后端返回 202 + job_id 时轮询直到结束；直接 200 时按同步结果返回（兼容旧后端）。
 * 其余选项（signal、onStatus、intervalMs、timeoutMs）透传给 pollAiJob。
 */
export async function runAiJob(submit, fetchJob, options = {}) {
  const res = await submit()
  if (!isQueuedJob(res)) return res?.data || {}
  options.onStatus?.({ status: res.data.status || 'queued', queued_ahead: 0 })
  return pollAiJob(() => fetchJob(res.data.job_id), options)
}

// 后台任务阶段 → 按钮上的进度文字；running 时返回 fallback（如「AI 翻译中」）
export function aiJobPhaseText(job, fallback) {
  if (job?.status !== 'queued') return fallback
  const ahead = Number(job.queued_ahead) || 0
  return ahead > 0 ? `已排队（前面 ${ahead} 个）` : '已排队'
}
