"""StockTrace 业务异常体系（ARC-05）。

- BadRequest：参数/业务校验失败 → HTTP 400。str(e) 是给用户看的提示文案，
  原样作为 detail 返回（前端直接展示），因此只放可公开的校验说明。
  同时继承 ValueError：过渡期内旧的 `except ValueError` 照样能接住。
- UpstreamError：上游数据源失败（网络、限流、返回格式异常）→ HTTP 502；
  异常原文可能含上游 URL/响应片段，只进日志，不回显给客户端（SEC-07）。
  - UpstreamTimeout：超过请求总时限 → 504
  - UpstreamCircuitOpen：同一上游连续超时熔断中 → 503

本模块不依赖项目内其他模块，任何层都可以 import。
"""


class StockTraceError(Exception):
    """StockTrace 业务异常基类。"""

    status_code = 500
    default_detail = '服务器内部错误，请稍后重试'


class BadRequest(StockTraceError, ValueError):
    """参数或业务校验失败（400）。"""

    status_code = 400
    default_detail = '请求参数不合法'


class UpstreamError(StockTraceError):
    """上游数据源失败（502）。"""

    status_code = 502
    default_detail = '上游数据获取失败，请稍后重试'


class UpstreamTimeout(UpstreamError):
    """上游请求超过总时限（504）；args[0] 为接口标签。"""

    status_code = 504
    default_detail = '上游数据获取超时，请稍后重试'


class UpstreamCircuitOpen(UpstreamError):
    """上游连续超时、熔断中（503）；args[0] 为接口标签。"""

    status_code = 503
    default_detail = '上游连续超时，暂停访问，请稍后再试'
