"""DRF 全局异常处理（ARC-05 步骤 5c；settings.REST_FRAMEWORK['EXCEPTION_HANDLER']）。

映射规则：
- DRF 自身异常（ValidationError、NotFound、ParseError 等）：沿用 DRF 默认处理；
- BadRequest → 400，detail 为其文案（业务校验提示，前端直接展示，保持原样）；
- UpstreamError 及子类 → 502 / 504（UpstreamTimeout）/ 503（UpstreamCircuitOpen）；
- 其他未处理异常 → 500。
除 BadRequest 外，响应体只给分类文案和 error_id，不回显异常原文（可能含上游 URL、
内网地址、文件路径，SEC-07）；原文与堆栈进日志，按 error_id 定位。
"""

import logging
import uuid

from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler, set_rollback

from ..errors import BadRequest, StockTraceError, UpstreamCircuitOpen, UpstreamError, UpstreamTimeout

logger = logging.getLogger(__name__)

_INTERNAL_DETAIL = StockTraceError.default_detail


def _where(context):
    request = context.get('request')
    view = context.get('view')
    path = getattr(request, 'path', '?')
    return f'{getattr(view, "__class__", type(None)).__name__} {path}'


def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is not None:
        return response

    if isinstance(exc, BadRequest):
        set_rollback()
        return Response({'detail': str(exc) or exc.default_detail}, status=exc.status_code)

    error_id = uuid.uuid4().hex[:12]
    if isinstance(exc, UpstreamError):
        if isinstance(exc, (UpstreamTimeout, UpstreamCircuitOpen)):
            # 超时/熔断是可预期的上游状态，不打堆栈
            logger.warning('%s：%s error_id=%s', _where(context), type(exc).__name__, error_id)
        else:
            logger.exception('上游失败 %s error_id=%s', _where(context), error_id)
        status_code, detail = exc.status_code, exc.default_detail
    else:
        logger.exception('未处理异常 %s error_id=%s', _where(context), error_id)
        status_code, detail = 500, _INTERNAL_DETAIL
    set_rollback()
    return Response(
        {'detail': f'{detail}（错误编号 {error_id}）', 'error_id': error_id},
        status=status_code,
    )
