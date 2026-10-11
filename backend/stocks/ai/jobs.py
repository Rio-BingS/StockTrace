"""AI 后台任务（P1 遗留：AI 调用异步化的后端最小可行版本）。

同步 AI 调用一次占一个 gunicorn 请求线程几十秒（1 worker × 2 线程），两个请求就能占满全站。
异步模式下视图只做校验与额度预占，立即返回 202 + job_id，LLM 调用放到本模块的单线程执行器里
排队执行，前端轮询 GET /api/ai/jobs/<job_id>/ 取结果。

局限（见 P2 说明）：任务状态存在进程内存里——
- 只适用于 GUNICORN_WORKERS=1（当前生产配置）；多 worker 时轮询可能打到别的 worker 而 404；
- 进程重启会丢失排队/进行中的任务（额度占位流水已写库，按失败计入每日上限）；
- 结果保留 JOB_TTL_SECONDS，最多 MAX_JOBS 条，过期即 404。
需要跨进程/持久化时，把任务状态与结果落到 AiCallLog（需模型字段 status/result，归数据方向）。
"""
import logging
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from django.db import close_old_connections

logger = logging.getLogger(__name__)

JOB_TTL_SECONDS = 3600
MAX_JOBS = 200

STATUS_QUEUED = 'queued'
STATUS_RUNNING = 'running'
STATUS_DONE = 'done'
STATUS_FAILED = 'failed'

_lock = threading.Lock()
_jobs = OrderedDict()
_executor = None


def _get_executor():
    # 单线程：AI 并发上限默认 1，多开线程只会在信号量上排队；懒创建，导入时不起线程
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='ai-job')
        return _executor


def _prune(now):
    """删除过期的已完成任务，并把总数压到 MAX_JOBS 以内（只淘汰已完成的）。"""
    for job_id in list(_jobs):
        job = _jobs[job_id]
        finished = job['status'] in (STATUS_DONE, STATUS_FAILED)
        if finished and now - (job.get('finished_at') or job['created_at']) > JOB_TTL_SECONDS:
            del _jobs[job_id]
    if len(_jobs) > MAX_JOBS:
        for job_id in list(_jobs):
            if len(_jobs) <= MAX_JOBS:
                break
            if _jobs[job_id]['status'] in (STATUS_DONE, STATUS_FAILED):
                del _jobs[job_id]


def _update(job_id, **fields):
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(fields)


def submit(kind, fn, meta=None):
    """提交任务，返回 job_id。fn() 在后台线程执行，返回 (http_status, body dict)。"""
    job_id = uuid.uuid4().hex
    now = time.time()
    with _lock:
        _prune(now)
        _jobs[job_id] = {
            'job_id': job_id,
            'kind': kind,
            'status': STATUS_QUEUED,
            'created_at': now,
            'meta': dict(meta or {}),
        }
    _get_executor().submit(_run, job_id, fn)
    return job_id


def _run(job_id, fn):
    _update(job_id, status=STATUS_RUNNING, started_at=time.time())
    try:
        http_status, body = fn()
        status = STATUS_DONE if http_status < 400 else STATUS_FAILED
        _update(job_id, status=status, http_status=http_status, result=body,
                finished_at=time.time())
    except Exception:
        logger.exception('AI 后台任务 %s 执行失败', job_id)
        _update(job_id, status=STATUS_FAILED, http_status=500,
                result={'detail': 'AI 任务执行失败，请稍后重试', 'error': 'internal'},
                finished_at=time.time())
    finally:
        # 后台线程用完数据库连接要关掉，否则 SQLite 连接随线程泄漏
        close_old_connections()


def get(job_id):
    """返回任务快照（dict 副本）；不存在或已过期返回 None。排队中的附带前面还有几个。"""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        snap = dict(job)
        if snap['status'] == STATUS_QUEUED:
            ahead = 0
            for other in _jobs.values():
                if other is job:
                    break
                if other['status'] in (STATUS_QUEUED, STATUS_RUNNING):
                    ahead += 1
            snap['queued_ahead'] = ahead
    return snap

