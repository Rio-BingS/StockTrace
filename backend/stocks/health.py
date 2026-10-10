"""健康检查 GET /api/health/（REL-02）。

返回：数据库是否可用、调度器心跳（独立进程写的 JSON 文件，见 scheduler.write_heartbeat）、
各定时任务下次运行时间与最近一次结果、最近一次市场日度快照日期。
- 全部正常 → 200 {"status": "ok"}；数据库不可用或调度心跳过期 → 503 {"status": "degraded"}。
- STOCKTRACE_SCHEDULER=0（人为关闭调度）时调度不计入失败。
- 只做轻量查询（SELECT 1 + 一次按索引取最新日期），供本机 cron / uptime-kuma 每分钟探测。
- nginx 只允许 127.0.0.1 访问；外部探测请经 SSH 或本机脚本。异常细节只写日志，不回显。
"""

import logging
import os
from datetime import datetime

from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)


def _check_db():
    try:
        with connection.cursor() as cur:
            cur.execute('SELECT 1')
            cur.fetchone()
        return {'ok': True}
    except Exception:
        logger.exception('健康检查：数据库不可用')
        return {'ok': False, 'error': 'database unavailable'}


def _latest_snapshot_date():
    try:
        from .models import MarketDailySnapshot

        d = MarketDailySnapshot.objects.order_by('-trade_date').values_list('trade_date', flat=True).first()
        return d.isoformat() if d else None
    except Exception:
        logger.exception('健康检查：读取最新市场快照日期失败')
        return None


def _check_scheduler():
    from .scheduler import read_heartbeat

    enabled = os.environ.get('STOCKTRACE_SCHEDULER', '1').lower() not in ('0', 'false', 'no', 'off')
    stale_after = int(getattr(settings, 'STOCKTRACE_HEARTBEAT_STALE_SECONDS', 180))
    hb = read_heartbeat()
    info = {'enabled': enabled, 'alive': False, 'stale_after_seconds': stale_after}
    if not hb:
        info['error'] = 'no heartbeat'
        return info
    age = None
    try:
        updated = datetime.fromisoformat(hb['updated_at'])
        age = max(0, int((timezone.now() - updated).total_seconds()))
    except (KeyError, TypeError, ValueError):
        pass
    info.update({
        'alive': bool(hb.get('running')) and age is not None and age <= stale_after,
        'running': bool(hb.get('running')),
        'mode': hb.get('mode'),
        'pid': hb.get('pid'),
        'started_at': hb.get('started_at'),
        'heartbeat_at': hb.get('updated_at'),
        'heartbeat_age_seconds': age,
        'jobs': hb.get('jobs') or [],
        'last_runs': hb.get('last_runs') or {},
    })
    return info


@require_GET
def health(request):
    db = _check_db()
    sched = _check_scheduler()
    ok = db['ok'] and (sched['alive'] or not sched['enabled'])
    body = {
        'status': 'ok' if ok else 'degraded',
        'time': timezone.now().isoformat(),
        'db': db,
        'scheduler': sched,
        'latest_market_snapshot': _latest_snapshot_date() if db['ok'] else None,
    }
    return JsonResponse(body, status=200 if ok else 503, json_dumps_params={'ensure_ascii': False})
