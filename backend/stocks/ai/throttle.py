"""AI 调用节流：同股冷却 + 每日上限。

安全模型是 Nginx Basic Auth（DRF AllowAny），AI 接口比行情接口慢且贵，
一旦 Basic Auth 泄露，节流是防止额度被烧穿的最后防线。

调用流程（AIX-06）：reserve_call() 在同一个事务里"检查节流 + 写占位流水"，
调用结束后 finish_call() 回填结果。占位流水在调用前就存在，并发请求也会被计入，
不再是"先查、调用完才写"的竞态窗口。
"""
import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import AiCallLog

logger = logging.getLogger(__name__)

# 服务商连通性测试（test 端点）单独轻量频控，不占用正式 AI 功能的每日总额度
PROVIDER_TEST_PURPOSE = 'provider_test'
PROVIDER_TEST_PER_MINUTE = 5
PROVIDER_TEST_DAILY_LIMIT = 30

# 进程内互斥：同一进程的线程之间串行"检查 + 占位"。跨进程依赖 settings 里
# SQLite 的 BEGIN IMMEDIATE（事务开始即拿写锁）
_reserve_lock = threading.Lock()


def stock_cooldown_seconds():
    """同一只股票两次分析的最小间隔（秒）；调用时读取配置，不在导入时固化。"""
    return int(getattr(settings, 'STOCKTRACE_AI_STOCK_COOLDOWN', 60))


def daily_limit():
    """每日 AI 调用总上限（所有用途合计，不含连通性测试）；调用时读取配置。"""
    return int(getattr(settings, 'STOCKTRACE_AI_DAILY_LIMIT', 100))


def _today_start(now):
    # 按本地时区（Asia/Shanghai）零点算"今天"；直接 replace UTC 时间会导致
    # 上限在北京时间早上 8 点才重置
    return timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)


def _has_field(name):
    try:
        AiCallLog._meta.get_field(name)
        return True
    except Exception:  # noqa: BLE001  FieldDoesNotExist
        return False


def _cooldown_qs(qs):
    """冷却只算成功与进行中的调用；失败调用不触发冷却（AIX-06）。

    "进行中"按数据库 status 判断，多个 worker 之间同样可见。
    """
    return qs.filter(status__in=(AiCallLog.STATUS_PENDING, AiCallLog.STATUS_SUCCESS))


def check_throttle(purpose, stock=None):
    """返回 (是否放行, 拒绝原因)。拒绝原因直接可展示给用户。"""
    now = timezone.now()
    cooldown = stock_cooldown_seconds()
    if stock is not None and cooldown > 0:
        recent = _cooldown_qs(AiCallLog.objects.filter(
            stock=stock,
            purpose=purpose,
            created_at__gte=now - timedelta(seconds=cooldown),
        )).exists()
        if recent:
            return False, f'同一股票 {cooldown} 秒内已分析过，请稍后再试'

    limit = daily_limit()
    # 每日上限计入失败调用：上游可能已计费
    daily_count = AiCallLog.objects.filter(created_at__gte=_today_start(now)).exclude(
        purpose=PROVIDER_TEST_PURPOSE
    ).count()
    if daily_count >= limit:
        return False, f'已达今日 AI 调用上限（{limit} 次），请明日再试'
    return True, None


def check_provider_test_throttle():
    """test 端点频控：每分钟与每日上限（所有服务商合计）。返回 (是否放行, 拒绝原因)。"""
    now = timezone.now()
    qs = AiCallLog.objects.filter(purpose=PROVIDER_TEST_PURPOSE)
    if qs.filter(created_at__gte=now - timedelta(seconds=60)).count() >= PROVIDER_TEST_PER_MINUTE:
        return False, f'连通性测试过于频繁（每分钟最多 {PROVIDER_TEST_PER_MINUTE} 次），请稍后再试'
    if qs.filter(created_at__gte=_today_start(now)).count() >= PROVIDER_TEST_DAILY_LIMIT:
        return False, f'已达今日连通性测试上限（{PROVIDER_TEST_DAILY_LIMIT} 次），请明日再试'
    return True, None


def reserve_call(purpose, provider=None, stock=None):
    """原子地检查节流并写入占位流水。返回 (占位流水, None) 或 (None, 拒绝原因)。"""
    with _reserve_lock, transaction.atomic():
        if purpose == PROVIDER_TEST_PURPOSE:
            allowed, reason = check_provider_test_throttle()
        else:
            allowed, reason = check_throttle(purpose, stock=stock)
        if not allowed:
            return None, reason
        log = AiCallLog.objects.create(
            purpose=purpose, provider=provider, stock=stock,
            success=False, status=AiCallLog.STATUS_PENDING,
        )
    return log, None


def error_text(exc):
    """失败原因摘要：只含通用分类与上游状态码，不含上游响应体（SEC-07），截断到 200 字。"""
    if exc is None:
        return ''
    kind = getattr(exc, 'kind', None) or (
        'config' if isinstance(exc, ValueError) else type(exc).__name__)
    code = getattr(exc, 'status_code', None)
    text = f'{kind} HTTP {code}' if code else str(kind)
    return text[:200]


def finish_call(log, success, usage=None, provider=None, error=None):
    """回填占位流水的结果；失败流水保留（计入每日上限，不计冷却）。

    可观测性（AIX-08）：耗时按占位写入时刻计算，模型取实际服务商。model/latency_ms/
    error/status 字段待模型迁移补上后自动落库；在此之前同样内容写一行 INFO 日志。
    """
    if provider is not None:
        log.provider = provider
    provider = log.provider
    model = (getattr(provider, 'model', '') or '')[:100]
    latency_ms = None
    if log.created_at:
        latency_ms = max(0, int((timezone.now() - log.created_at).total_seconds() * 1000))
    err = error_text(error) if not success else ''
    log.success = bool(success)
    log.prompt_tokens = (usage or {}).get('prompt_tokens')
    log.completion_tokens = (usage or {}).get('completion_tokens')
    log.status = AiCallLog.STATUS_SUCCESS if success else AiCallLog.STATUS_FAILED
    update = ['success', 'status', 'prompt_tokens', 'completion_tokens', 'provider']
    optional = {
        'model': model,
        'latency_ms': latency_ms,
        'error': err,
    }
    for name, value in optional.items():
        if _has_field(name):
            setattr(log, name, value)
            update.append(name)
    log.save(update_fields=update)
    logger.info(
        'AI 调用 purpose=%s provider=%s model=%s success=%s latency_ms=%s '
        'prompt_tokens=%s completion_tokens=%s error=%s',
        log.purpose, getattr(provider, 'pk', None), model, log.success, latency_ms,
        log.prompt_tokens, log.completion_tokens, err or '-',
    )


def cancel_call(log):
    """撤销占位（并发已满等未真正调用上游的情况）：不占额度。"""
    log.delete()


def log_call(purpose, provider=None, stock=None, success=True, usage=None):
    """直接写一条已完成的流水（测试与兼容旧调用方用）。"""
    fields = dict(
        purpose=purpose,
        provider=provider,
        stock=stock,
        success=success,
        prompt_tokens=(usage or {}).get('prompt_tokens'),
        completion_tokens=(usage or {}).get('completion_tokens'),
        status=AiCallLog.STATUS_SUCCESS if success else AiCallLog.STATUS_FAILED,
    )
    return AiCallLog.objects.create(**fields)
