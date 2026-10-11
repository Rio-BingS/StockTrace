"""API 写请求来源校验（SEC-03）

浏览器会为跨站的顶层表单 POST 自动附带已缓存的 Nginx Basic Auth 凭据，
而 DRF 视图本身 csrf_exempt、无认证类，所以 Django 的 CsrfViewMiddleware 管不到 /api/。
这里只对 /api/ 的非安全方法做轻量校验，规则按顺序：

1. 带 Origin 且不受信（含 sandbox iframe / data: 页面发出的 ``null``）→ 403；
2. Sec-Fetch-Site 为 cross-site / same-site 且 Origin 不受信 → 403；
3. 带 ``X-Requested-With: XMLHttpRequest``（前端 axios 实例统一加）→ 放行。
   该头不在 CORS 安全列表，跨站页面要发就必须先过预检，而预检受 CORS 白名单约束；
4. 没有任何浏览器信号（Origin / Sec-Fetch-* / Referer 都没有：curl、管理脚本、测试客户端）→ 放行；
5. 其余（浏览器发出但缺少该头）→ STOCKTRACE_CSRF_REQUIRE_HEADER 开启时 403（默认开）。

受信来源 = settings.STOCKTRACE_TRUSTED_ORIGINS + 请求自身的源（同源）；
DEBUG 下额外信任 Vite 开发服务器（localhost:5173，它代理 /api 时不改写 Origin）。
"""
from django.conf import settings
from django.core.exceptions import DisallowedHost
from django.http import JsonResponse

SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS', 'TRACE'})
_DEV_ORIGINS = frozenset({'http://localhost:5173', 'http://127.0.0.1:5173'})
_CROSS_SITE = frozenset({'cross-site', 'same-site'})


def _trusted_origins(request):
    trusted = set(getattr(settings, 'STOCKTRACE_TRUSTED_ORIGINS', []))
    if settings.DEBUG:
        trusted |= _DEV_ORIGINS
    try:
        trusted.add(f'{request.scheme}://{request.get_host()}')
    except DisallowedHost:
        pass
    return trusted


def _forbidden(reason):
    return JsonResponse({'detail': f'请求来源校验失败：{reason}'}, status=403)


class ApiOriginCheckMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method not in SAFE_METHODS and request.path.startswith('/api/'):
            denied = self.check(request)
            if denied is not None:
                return denied
        return self.get_response(request)

    @staticmethod
    def check(request):
        headers = request.headers
        origin = headers.get('Origin')
        fetch_site = (headers.get('Sec-Fetch-Site') or '').lower()
        origin_trusted = False
        if origin is not None:
            origin_trusted = origin.rstrip('/') in _trusted_origins(request)
            if not origin_trusted:
                return _forbidden('不受信的 Origin')
        if fetch_site in _CROSS_SITE and not origin_trusted:
            return _forbidden('跨站请求')
        if headers.get('X-Requested-With') == 'XMLHttpRequest':
            return None
        has_browser_signal = (
            origin is not None
            or fetch_site
            or headers.get('Sec-Fetch-Mode')
            or headers.get('Referer')
        )
        if not has_browser_signal:
            return None
        if getattr(settings, 'STOCKTRACE_CSRF_REQUIRE_HEADER', True):
            return _forbidden('缺少 X-Requested-With 头')
        return None
