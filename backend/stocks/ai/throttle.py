"""AI 调用节流：同股冷却 + 每日上限。

安全模型是 Nginx Basic Auth（DRF AllowAny），AI 接口比行情接口慢且贵，
一旦 Basic Auth 泄露，节流是防止额度被烧穿的最后防线。

调用流程（AIX-06）：reserve_call() 在同一个事务里"检查节流 + 写占位流水"，
调用结束后 finish_call() 回填结果。占位流水在调用前就存在，并发请求也会被计入，
不再是"先查、调用完才写"的竞态窗口。
"""
import threading
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import AiCallLog

# 服务商连通性测试（test 端点）单独轻量频控，不占用正式 AI 功能的每日总额度
PROVIDER_TEST_PURPOSE = 'provider_test'
PROVIDER_TEST_PER_MINUTE = 5
PROVIDER_TEST_DAILY_LIMIT = 30

# 进程内互斥：同一进程的线程之间串行"检查 + 占位"。跨进程依赖 settings 里
# SQLite 的 BEGIN IMMEDIATE（事务开始即拿写锁）
_reserve_lock = threading.Lock()
# 进行中的占位流水 pk。模型还没有 status 字段时，用它区分"进行中"与"已失败"
_inflight = set()
_inflight_lock = threading.Lock()


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
    """冷却只算成功与进行中的调用；失败调用不触发冷却（AIX-06）。"""
    from django.db.models import Q
    if _has_field('status'):
        return qs.filter(status__in=('pending', 'success'))
    with _inflight_lock:
        inflight = list(_inflight)
    return qs.filter(Q(success=True) | Q(pk__in=inflight))


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
        fields = {'purpose': purpose, 'provider': provider, 'stock': stock, 'success': False}
        if _has_field('status'):
            fields['status'] = 'pending'
        log = AiCallLog.objects.create(**fields)
    with _inflight_lock:
        _inflight.add(log.pk)
    return log, None


def finish_call(log, success, usage=None, provider=None):
    """回填占位流水的结果；失败流水保留（计入每日上限，不计冷却）。"""
    with _inflight_lock:
        _inflight.discard(log.pk)
    log.success = bool(success)
    log.prompt_tokens = (usage or {}).get('prompt_tokens')
    log.completion_tokens = (usage or {}).get('completion_tokens')
    update = ['success', 'prompt_tokens', 'completion_tokens']
    if provider is not None:
        log.provider = provider
        update.append('provider')
    if _has_field('status'):
        log.status = 'success' if success else 'failed'
        update.append('status')
    log.save(update_fields=update)


def cancel_call(log):
    """撤销占位（并发已满等未真正调用上游的情况）：不占额度。"""
    with _inflight_lock:
        _inflight.discard(log.pk)
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
    )
    if _has_field('status'):
        fields['status'] = 'success' if success else 'failed'
    return AiCallLog.objects.create(**fields)
