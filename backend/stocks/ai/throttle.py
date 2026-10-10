"""AI 调用节流：同股冷却 + 每日上限。

安全模型是 Nginx Basic Auth（DRF AllowAny），AI 接口比行情接口慢且贵，
一旦 Basic Auth 泄露，节流是防止额度被烧穿的最后防线。
"""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from ..models import AiCallLog

# 同一只股票两次分析的最小间隔（秒）
STOCK_COOLDOWN_SECONDS = 60
# 每日 AI 调用总上限（所有用途合计）
DAILY_LIMIT = int(getattr(settings, 'STOCKTRACE_AI_DAILY_LIMIT', 100))

# 服务商连通性测试（test 端点）单独轻量频控，不占用上面的每日总额度
PROVIDER_TEST_PURPOSE = 'provider_test'
PROVIDER_TEST_PER_MINUTE = 5
PROVIDER_TEST_DAILY_LIMIT = 30


def _today_start(now):
    # 按本地时区（Asia/Shanghai）零点算"今天"
    return timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)


def check_throttle(purpose, stock=None):
    """返回 (是否放行, 拒绝原因)。拒绝原因直接可展示给用户。"""
    now = timezone.now()
    if stock is not None:
        recent = AiCallLog.objects.filter(
            stock=stock,
            purpose=purpose,
            created_at__gte=now - timedelta(seconds=STOCK_COOLDOWN_SECONDS),
        ).exists()
        if recent:
            return False, f'同一股票 {STOCK_COOLDOWN_SECONDS} 秒内已分析过，请稍后再试'

    # 按本地时区（Asia/Shanghai）零点算"今天"；直接 replace UTC 时间会导致
    # 上限在北京时间早上 8 点才重置
    today_start = timezone.localtime(now).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    daily_count = AiCallLog.objects.filter(created_at__gte=today_start).exclude(
        purpose=PROVIDER_TEST_PURPOSE
    ).count()
    if daily_count >= DAILY_LIMIT:
        return False, f'已达今日 AI 调用上限（{DAILY_LIMIT} 次），请明日再试'
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


def log_call(purpose, provider=None, stock=None, success=True, usage=None):
    AiCallLog.objects.create(
        purpose=purpose,
        provider=provider,
        stock=stock,
        success=success,
        prompt_tokens=(usage or {}).get('prompt_tokens'),
        completion_tokens=(usage or {}).get('completion_tokens'),
    )
