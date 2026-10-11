"""AI 后台任务（AIX-08 收尾：任务状态与结果落库到 AiJob）。

同步 AI 调用一次占一个 gunicorn 请求线程几十秒（1 worker × 2 线程），两个请求就能占满全站。
异步模式下视图只做校验与额度预占，写一行 pending 任务后立即返回 202 + job_id；
前端轮询 GET /api/ai/jobs/<job_id>/ 取结果。

执行端：Web 进程（gunicorn worker）内懒启动的单个后台线程。
- 认领：条件 UPDATE（status=pending → running，带 owner 与租约），影响行数为 1 才算抢到；
  SQLite（BEGIN IMMEDIATE 串行写）与支持 skip_locked 的数据库都不会重复执行。
- 全局并发：认领前统计租约内的 running 数，不超过 STOCKTRACE_AI_MAX_CONCURRENCY（跨 worker）。
- 唤醒：提交任务的进程在事务提交后立即唤醒本进程线程；其余时间每 POLL_SECONDS 查一次库，
  连续 IDLE_EXIT_SECONDS 没有待办就退出，下次提交或轮询时再起。
- 恢复：进程重启后 pending 任务由任一 worker 继续执行（轮询请求会拉起线程）；
  running 超过租约的视为执行进程已退出，标为失败（上游可能已计费，不自动重跑）；
  pending 超过 PENDING_TIMEOUT 仍无人执行的标为失败并撤销额度占位。
- 保留期：完成/失败的任务保留 JOB_TTL_SECONDS（过期即 404），提交时与收盘清理时删除旧行。

为什么不放在 stocktrace-scheduler 进程：AI 调用是纯 I/O，放 Web 进程可以提交即执行、
不依赖调度进程存活，也不和 akshare/pandas 抢 700M 内存；调度进程只负责每日兜底清理。
"""
import logging
import os
import socket
import threading
import time
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import OperationalError, close_old_connections, transaction
from django.db.models import F
from django.utils import timezone

from ..models import AiJob

logger = logging.getLogger(__name__)

# 对外（轮询 JSON）的状态名保持不变：库里的 pending 对外仍叫 queued
STATUS_QUEUED = 'queued'
STATUS_RUNNING = AiJob.STATUS_RUNNING
STATUS_DONE = AiJob.STATUS_DONE
STATUS_FAILED = AiJob.STATUS_FAILED
_PUBLIC_STATUS = {AiJob.STATUS_PENDING: STATUS_QUEUED}

# 后台线程空闲时查库的间隔与空闲退出时间（秒）
POLL_SECONDS = 2.0
IDLE_EXIT_SECONDS = 120.0
# 恢复检查（租约过期/排队超时）的最小间隔（秒）
RECOVER_INTERVAL_SECONDS = 15.0
# 一次认领时最多看几个候选（被别的 worker 抢走就试下一个）
_CLAIM_CANDIDATES = 5

_FINISHED = (AiJob.STATUS_DONE, AiJob.STATUS_FAILED)
_ACTIVE = (AiJob.STATUS_PENDING, AiJob.STATUS_RUNNING)

_handlers = {}


def register(kind):
    """注册任务处理函数：handler(params) -> (http_status, body dict)。"""
    def deco(fn):
        _handlers[kind] = fn
        return fn
    return deco


def _get_handler(kind):
    if kind not in _handlers:
        # 处理函数定义在视图模块（复用 LLM 调用与错误出口），按需导入完成注册
        from . import views  # noqa: F401
    return _handlers.get(kind)


# ---- 配置（调用时读取，便于 override_settings）----

def _setting(name, default):
    try:
        return float(getattr(settings, name, default))
    except (TypeError, ValueError):
        return float(default)


def job_ttl_seconds():
    """已完成任务的保留时长（秒）：过期后轮询 404，清理时删除。"""
    return _setting('STOCKTRACE_AI_JOB_TTL', 3600)


def lease_seconds():
    """执行租约（秒）：等并发名额 + LLM 总时长 + 余量；超过仍是 running 视为执行进程已退出。"""
    total = _setting('STOCKTRACE_LLM_TOTAL_TIMEOUT', 90)
    return _setting('STOCKTRACE_AI_JOB_LEASE', total * 2 + 60)


def pending_timeout_seconds():
    """pending 任务最长排队时长（秒）：超过仍无人执行则标失败、撤销额度占位。"""
    return _setting('STOCKTRACE_AI_JOB_PENDING_TIMEOUT', 600)


def _max_concurrency():
    try:
        return max(1, int(getattr(settings, 'STOCKTRACE_AI_MAX_CONCURRENCY', 1)))
    except (TypeError, ValueError):
        return 1


def _worker_enabled():
    return bool(getattr(settings, 'STOCKTRACE_AI_JOB_WORKER', True))


# ---- 提交与查询 ----

def submit(kind, params=None):
    """写入一条 pending 任务并唤醒执行线程，返回 job_id。

    params 必须可 JSON 序列化；处理函数据此重建调用（不能传闭包，任务可能由别的进程执行）。
    """
    now = timezone.now()
    try:
        prune(now)
    except Exception:
        logger.exception('AI 后台任务清理失败')
    job = AiJob.objects.create(job_id=uuid.uuid4().hex, kind=kind, params=dict(params or {}))
    # 事务提交后才对别的连接可见；没有外层事务时立即执行
    transaction.on_commit(ensure_worker)
    return job.job_id


def get(job_id):
    """返回任务快照 dict；不存在或已过期返回 None。排队中的附带前面还有几个。

    快照字段与旧版一致：job_id、kind、status（queued/running/done/failed）、
    queued_ahead（仅排队中）、http_status 与 result（仅完成/失败）。
    """
    job = AiJob.objects.filter(job_id=str(job_id)[:32]).first()
    if job is None:
        return None
    if job.status in _FINISHED and _expired(job, timezone.now()):
        return None
    snap = {
        'job_id': job.job_id,
        'kind': job.kind,
        'status': _PUBLIC_STATUS.get(job.status, job.status),
    }
    if job.status == AiJob.STATUS_PENDING:
        snap['queued_ahead'] = AiJob.objects.filter(status__in=_ACTIVE, id__lt=job.id).count()
    if job.status in _FINISHED:
        snap['http_status'] = job.http_status
        snap['result'] = job.result
    else:
        # 进程重启后没人提交新任务时，由轮询请求拉起执行线程继续处理
        ensure_worker()
    return snap


def _expired(job, now):
    finished = job.finished_at or job.created_at
    return now - finished > timedelta(seconds=job_ttl_seconds())


def prune(now=None):
    """删除超过保留期的已完成任务，以及早已无人处理的陈旧行。返回删除条数。"""
    now = now or timezone.now()
    ttl = timedelta(seconds=job_ttl_seconds())
    deleted, _ = AiJob.objects.filter(status__in=_FINISHED, finished_at__lt=now - ttl).delete()
    # 兜底：一天前创建的任何任务都已无意义（异常中断未回填 finished_at 的也一并删除）
    stale, _ = AiJob.objects.filter(
        created_at__lt=now - max(ttl, timedelta(days=1))).delete()
    return deleted + stale


# ---- 认领与执行 ----

def _new_owner():
    return f'{socket.gethostname()[:40]}:{os.getpid()}:{uuid.uuid4().hex[:8]}'


def claim_next(owner=None):
    """抢占一条最早的 pending 任务，返回 AiJob（已置 running）或 None。

    全局并发已满（租约内 running ≥ 上限）时不认领。抢占用条件 UPDATE：
    只有 status 仍为 pending 的那一行会被更新，别的 worker 抢走时影响行数为 0。
    """
    owner = owner or _new_owner()
    now = timezone.now()
    with transaction.atomic():
        running = AiJob.objects.filter(status=AiJob.STATUS_RUNNING, lease_until__gt=now).count()
        if running >= _max_concurrency():
            return None
        # 支持 skip_locked 的数据库跳过别人正在认领的行；SQLite 上 select_for_update 不生效，
        # 由 BEGIN IMMEDIATE 串行写 + 下面的条件 UPDATE 保证互斥
        candidates = list(
            AiJob.objects.select_for_update(skip_locked=True)
            .filter(status=AiJob.STATUS_PENDING).order_by('id')
            .values_list('id', flat=True)[:_CLAIM_CANDIDATES]
        )
        for pk in candidates:
            won = AiJob.objects.filter(pk=pk, status=AiJob.STATUS_PENDING).update(
                status=AiJob.STATUS_RUNNING, owner=owner, started_at=now,
                lease_until=now + timedelta(seconds=lease_seconds()),
                attempts=F('attempts') + 1,
            )
            if won:
                return AiJob.objects.get(pk=pk)
    return None


def run_job(job):
    """执行已认领的任务并回填结果。处理函数异常时记为失败（500 通用文案）。"""
    handler = _get_handler(job.kind)
    try:
        if handler is None:
            raise LookupError(f'未知的 AI 任务类型 {job.kind}')
        http_status, body = handler(dict(job.params or {}))
        status = AiJob.STATUS_DONE if http_status < 400 else AiJob.STATUS_FAILED
        error = None if status == AiJob.STATUS_DONE else str((body or {}).get('error') or '')[:200]
    except Exception:
        logger.exception('AI 后台任务 %s 执行失败', job.job_id)
        http_status, status, error = 500, AiJob.STATUS_FAILED, 'internal'
        body = {'detail': 'AI 任务执行失败，请稍后重试', 'error': 'internal'}
    # 只回填自己认领的那次：租约过期被标失败后本进程仍跑完的，以真实结果覆盖
    updated = _retry_locked(lambda: AiJob.objects.filter(pk=job.pk, owner=job.owner).update(
        status=status, http_status=http_status, result=body, error=error or None,
        finished_at=timezone.now(), lease_until=None,
    ))
    if not updated:
        logger.warning('AI 后台任务 %s 已被删除或改由其他执行者处理，结果丢弃', job.job_id)
    return status


def _retry_locked(fn, attempts=5):
    """写库遇到锁冲突（SQLite database is locked）时短暂退避重试。

    回填结果写不进去任务会一直停在 running，直到租约过期被误判为中断，所以这里多试几次。
    """
    for i in range(attempts):
        try:
            return fn()
        except OperationalError as exc:
            if 'locked' not in str(exc) or i == attempts - 1:
                raise
            time.sleep(0.1 * (i + 1))


def run_pending(max_jobs=None):
    """在当前线程认领并执行待办任务，直到没有可认领的（测试与手动排障用）。返回执行条数。"""
    done = 0
    owner = _new_owner()
    while max_jobs is None or done < max_jobs:
        job = claim_next(owner)
        if job is None:
            break
        run_job(job)
        done += 1
    return done


# ---- 恢复 ----

_INTERRUPTED_BODY = {'detail': 'AI 任务因服务重启中断，请重新发起', 'error': 'interrupted'}
_EXPIRED_BODY = {'detail': 'AI 任务排队超时，请重新发起', 'error': 'expired'}


def recover_stale(now=None):
    """处理异常中断的任务，返回 (标失败的 running 数, 标失败的 pending 数)。

    - running 且租约已过期：执行进程已退出（重启/被杀）。上游可能已计费，不重跑；
      标失败，占位流水按失败回填（计入每日上限）。
    - pending 超过排队时限：没有任何进程执行过，标失败并撤销占位流水（不占额度）。
    """
    now = now or timezone.now()
    interrupted = _fail_where(
        AiJob.objects.filter(status=AiJob.STATUS_RUNNING, lease_until__lt=now),
        AiJob.STATUS_RUNNING, 500, _INTERRUPTED_BODY, now, call_log_action='fail')
    expired = _fail_where(
        AiJob.objects.filter(status=AiJob.STATUS_PENDING,
                             created_at__lt=now - timedelta(seconds=pending_timeout_seconds())),
        AiJob.STATUS_PENDING, 503, _EXPIRED_BODY, now, call_log_action='cancel')
    if interrupted or expired:
        logger.warning('AI 后台任务恢复：中断 %s 个，排队超时 %s 个', interrupted, expired)
    return interrupted, expired


def _fail_where(qs, from_status, http_status, body, now, call_log_action):
    count = 0
    for job in list(qs[:100]):
        # 条件更新：与认领/回填竞争时只有一方生效
        won = AiJob.objects.filter(pk=job.pk, status=from_status).update(
            status=AiJob.STATUS_FAILED, http_status=http_status, result=body,
            error=body['error'], finished_at=now, lease_until=None,
        )
        if not won:
            continue
        count += 1
        _settle_call_log(job.params or {}, call_log_action)
    return count


def _settle_call_log(params, action):
    """回填/撤销任务预占的 AiCallLog（仍是 pending 时才处理）。"""
    from ..models import AiCallLog
    from .llm import LlmError
    from .throttle import cancel_call, finish_call

    log_id = params.get('call_log_id')
    if not log_id:
        return
    log = AiCallLog.objects.filter(pk=log_id, status=AiCallLog.STATUS_PENDING).first()
    if log is None:
        return
    if action == 'cancel':
        cancel_call(log)
    else:
        finish_call(log, False, error=LlmError('AI 后台任务中断', kind='interrupted'))


# ---- 进程内执行线程 ----

_worker_lock = threading.Lock()
_worker_thread = None
_wake = threading.Event()


def ensure_worker():
    """确保本进程的执行线程在运行，并唤醒它立即检查待办。"""
    global _worker_thread
    if not _worker_enabled():
        return
    with _worker_lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(target=_worker_loop, name='ai-job', daemon=True)
            _worker_thread.start()
        _wake.set()


def _has_pending():
    return AiJob.objects.filter(status__in=_ACTIVE).exists()


def _worker_loop():
    global _worker_thread
    owner_prefix = _new_owner()
    idle_since = time.monotonic()
    last_recover = 0.0
    seq = 0
    try:
        while True:
            _wake.clear()
            job = None
            pending = False
            try:
                if time.monotonic() - last_recover >= RECOVER_INTERVAL_SECONDS:
                    recover_stale()
                    last_recover = time.monotonic()
                seq += 1
                job = claim_next(f'{owner_prefix}:{seq}')
                if job is not None:
                    run_job(job)
                else:
                    pending = _has_pending()
            except Exception:
                logger.exception('AI 后台任务线程异常')
            finally:
                # 每轮用完数据库连接就关：后台线程不经过请求结束信号，SQLite 连接会随线程泄漏
                close_old_connections()
            if job is not None or pending:
                if job is not None:
                    idle_since = time.monotonic()
                    continue
            elif time.monotonic() - idle_since >= IDLE_EXIT_SECONDS:
                with _worker_lock:
                    if not _wake.is_set():
                        _worker_thread = None
                        return
            _wake.wait(POLL_SECONDS)
    finally:
        close_old_connections()
