"""LLM 客户端：统一走 OpenAI 兼容 /chat/completions 协议。"""
import ipaddress
import json
import logging
import random
import socket
import threading
import time
from urllib.parse import urlsplit

import requests
from django.conf import settings

from .crypto import decrypt_api_key

logger = logging.getLogger(__name__)


class LlmError(Exception):
    """LLM 调用失败（网络/上游/配置）——上层如实报错，绝不伪造结果

    kind：通用错误分类（config/network/timeout/redirect/auth/rate_limit/upstream/
    bad_response/empty/truncated/busy），status_code：上游 HTTP 状态码（有则填）。
    message 只用于服务端日志，不含上游响应体；对外只给按 kind 的通用文案（SEC-07）。
    usage：上游已计费的 token 用量（empty/truncated 时可能有）。
    """

    def __init__(self, message, kind='upstream', status_code=None, usage=None):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.usage = usage


# ---- 全局并发上限（AIX-02）----
# 进程内信号量：gunicorn 只有少量线程，同步 LLM 调用一次占一个线程几十秒，
# 并发上限满了立即报 busy（视图返回 429），不排队、不占线程。
_slot_lock = threading.Lock()
_slot_sem = None
_slot_size = None


def _max_concurrency():
    try:
        return max(1, int(getattr(settings, 'STOCKTRACE_AI_MAX_CONCURRENCY', 1)))
    except (TypeError, ValueError):
        return 1


def _get_semaphore():
    """按当前配置取信号量；配置变化（测试 override_settings）时重建。"""
    global _slot_sem, _slot_size
    size = _max_concurrency()
    with _slot_lock:
        if _slot_sem is None or _slot_size != size:
            _slot_sem = threading.BoundedSemaphore(size)
            _slot_size = size
        return _slot_sem


class _AiSlot:
    """非阻塞占用一个 AI 并发名额；拿不到抛 LlmError(kind='busy')。"""

    def __enter__(self):
        self.sem = _get_semaphore()
        if not self.sem.acquire(blocking=False):
            raise LlmError('AI 并发已满', kind='busy')
        return self

    def __exit__(self, *exc):
        self.sem.release()
        return False


def _extract_text(data):
    """从 /chat/completions 响应 JSON 取正文；结构不对、空内容、截断都抛 LlmError（AIX-03）。"""
    usage = data.get('usage') if isinstance(data, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    usage = {
        'prompt_tokens': usage.get('prompt_tokens'),
        'completion_tokens': usage.get('completion_tokens'),
    }
    choices = data.get('choices') if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise LlmError('LLM 响应缺少 choices', kind='bad_response', status_code=200)
    choice = choices[0]
    msg = choice.get('message')
    if not isinstance(msg, dict):
        raise LlmError('LLM 响应缺少 message', kind='bad_response', status_code=200)
    content = msg.get('content')
    if content is not None and not isinstance(content, str):
        raise LlmError('LLM 响应 content 不是文本', kind='bad_response', status_code=200)
    if choice.get('finish_reason') == 'length':
        # 输出被 max_tokens 截断：半截内容不当作完整结果返回
        raise LlmError('LLM 输出被截断', kind='truncated', status_code=200, usage=usage)
    text = (content or '').strip()
    if not text:
        raise LlmError('LLM 返回空内容', kind='empty', status_code=200, usage=usage)
    return text, usage


def _retry_delay(attempt):
    # 随机退避：1s、2s…基数上加抖动，避免与上游限流窗口同步
    return min(8.0, (2 ** attempt) * random.uniform(0.5, 1.5))


def _ip_is_public(ip):
    # IPv4 映射的 IPv6（::ffff:127.0.0.1）按内嵌 IPv4 判断
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not (
        ip.is_multicast or ip.is_reserved or ip.is_unspecified
        or ip.is_loopback or ip.is_link_local or ip.is_private
    )


def validate_base_url(url):
    """校验 AI 服务商 base_url，返回规范化后的地址（去尾部 /）；不合法抛 ValueError。

    防 SEC-01：Key 只能发往 https 公网地址，拒绝私网/回环/链路本地/保留地址（含 DNS 解析结果）。
    - AI_ALLOW_HTTP_BASE_URL=True 时放行 http（仅限本地开发）；
    - AI_BASE_URL_ALLOWED_HOSTS 非空时主机必须在白名单内；白名单内的主机视为运维显式信任，
      跳过内网地址检查（用于本地开发如 127.0.0.1 上的 Ollama）。
    """
    url = (url or '').strip()
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError('接口地址格式不合法') from exc
    allow_http = bool(getattr(settings, 'AI_ALLOW_HTTP_BASE_URL', False))
    allowed_schemes = ('https', 'http') if allow_http else ('https',)
    if parts.scheme.lower() not in allowed_schemes:
        raise ValueError('接口地址必须以 https:// 开头')
    host = (parts.hostname or '').rstrip('.').lower()
    if not host:
        raise ValueError('接口地址缺少主机名')
    if parts.username is not None or parts.password is not None:
        raise ValueError('接口地址不能包含用户名/密码')
    if parts.query or parts.fragment:
        raise ValueError('接口地址不能包含查询参数或锚点')

    allowed_hosts = {
        h.strip().rstrip('.').lower()
        for h in getattr(settings, 'AI_BASE_URL_ALLOWED_HOSTS', []) or []
        if h and h.strip()
    }
    if allowed_hosts:
        if host not in allowed_hosts:
            raise ValueError(f'接口地址主机 {host} 不在白名单 AI_BASE_URL_ALLOWED_HOSTS 内')
        return url.rstrip('/')

    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(
                host, port or (443 if parts.scheme.lower() == 'https' else 80),
                proto=socket.IPPROTO_TCP,
            )
        except (OSError, UnicodeError) as exc:
            raise ValueError(f'接口地址主机 {host} 无法解析') from exc
        ips = [ipaddress.ip_address(info[4][0].split('%')[0]) for info in infos]
        if not ips:
            raise ValueError(f'接口地址主机 {host} 无法解析')
    for ip in ips:
        if not _ip_is_public(ip):
            raise ValueError('接口地址不能指向内网、回环、链路本地或保留地址')
    return url.rstrip('/')


class LlmClient:
    def __init__(self, provider):
        self.provider = provider
        self.base_url = provider.base_url.rstrip('/')
        self.api_key = decrypt_api_key(provider.api_key_encrypted)

    def chat(self, messages, temperature=0.3, max_tokens=2000, timeout=None,
             max_retries=None):
        """返回 (文本, usage dict)。失败抛 LlmError。

        受全局并发上限约束，满了立即抛 kind='busy'。
        """
        with _AiSlot():
            return self._chat(messages, temperature, max_tokens, timeout, max_retries)

    def _chat(self, messages, temperature, max_tokens, timeout=None, max_retries=None,
              extra=None):
        # 配置在调用时读取（不在导入时固化），便于运维改环境变量后重启即生效
        timeout = timeout or int(getattr(settings, 'STOCKTRACE_LLM_TIMEOUT', 60))
        if max_retries is None:
            max_retries = int(getattr(settings, 'STOCKTRACE_LLM_MAX_RETRIES', 1))
        # 跨重试的总时长上限：单次 timeout 只是连接/读间隔超时，不是总时长（AIX-02）
        total = float(getattr(settings, 'STOCKTRACE_LLM_TOTAL_TIMEOUT', 90))
        deadline = time.monotonic() + total

        # 调用前再次校验：库里可能有旧的不合规地址，且 DNS 解析结果可能已变化
        try:
            validate_base_url(self.base_url)
        except ValueError as exc:
            raise LlmError(f'接口地址不安全：{exc}', kind='config') from exc

        payload = {
            'model': self.provider.model,
            'messages': messages,
            'temperature': temperature,
            'max_tokens': max_tokens,
        }
        if extra:
            payload.update(extra)

        attempt = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LlmError('LLM 调用超过总时长上限', kind='timeout')
            try:
                return self._post_once(payload, min(timeout, remaining))
            except LlmError as exc:
                if not _retryable(exc) or attempt >= max_retries:
                    raise
                delay = _retry_delay(attempt)
                # 退避后剩余时间太少就不再重试，直接报本次的错误
                if deadline - time.monotonic() - delay < 1:
                    raise
                logger.info('LLM 调用失败将重试：kind=%s status=%s attempt=%s',
                            exc.kind, exc.status_code, attempt + 1)
                time.sleep(delay)
                attempt += 1

    def _post_once(self, payload, timeout):
        try:
            resp = requests.post(
                f'{self.base_url}/chat/completions',
                headers={
                    'Authorization': f'Bearer {self.api_key}',
                    'Content-Type': 'application/json',
                },
                json=payload,
                timeout=timeout,
                # 不跟随重定向：防止 30x 把带 Key 的请求转到内网或第三方
                allow_redirects=False,
            )
        except requests.ConnectTimeout as exc:
            # 连接阶段超时：请求未送达上游，可安全重试
            raise LlmError(f'LLM 连接超时：{exc}', kind='network') from exc
        except requests.Timeout as exc:
            raise LlmError(f'LLM 请求超时：{exc}', kind='timeout') from exc
        except requests.RequestException as exc:
            raise LlmError(f'LLM 请求失败：{exc}', kind='network') from exc

        if resp.status_code != 200:
            code = resp.status_code
            if 300 <= code < 400:
                # 重定向不跟随，也不回显 Location
                raise LlmError(f'LLM 上游返回重定向 {code}，已拒绝跟随',
                               kind='redirect', status_code=code)
            if code in (401, 403):
                kind = 'auth'
            elif code == 429:
                kind = 'rate_limit'
            else:
                kind = 'upstream'
            # 不把上游响应体放进异常（SEC-07），只在 debug 日志留一小段便于排障
            logger.debug('LLM 上游返回 %s：%s', code, (resp.text or '')[:300])
            raise LlmError(f'LLM 上游返回 {code}', kind=kind, status_code=code)

        try:
            data = resp.json()
        except ValueError as exc:
            raise LlmError('LLM 响应不是合法 JSON', kind='bad_response',
                           status_code=resp.status_code) from exc
        return _extract_text(data)

    def chat_json(self, messages, temperature=0.0, max_tokens=2000):
        """要求严格 JSON 对象输出并解析；解析失败抛 LlmError，不猜不修。

        先用 JSON mode（response_format=json_object）；服务商不支持（400）时退回普通模式。
        """
        with _AiSlot():
            try:
                text, usage = self._chat(
                    messages, temperature, max_tokens,
                    extra={'response_format': {'type': 'json_object'}},
                )
            except LlmError as exc:
                if exc.status_code != 400:
                    raise
                logger.info('服务商不支持 JSON mode，退回普通模式')
                text, usage = self._chat(messages, temperature, max_tokens)
        obj = _parse_json_object(text)
        if obj is None:
            raise LlmError('LLM 未返回合法 JSON 对象', kind='bad_response', usage=usage)
        return obj, usage


def _retryable(exc):
    # 只重试暂时性错误：限流、5xx、连接失败；读超时可能已在上游计费，不重试
    if exc.kind in ('rate_limit', 'network'):
        return True
    return exc.kind == 'upstream' and exc.status_code is not None and exc.status_code >= 500


def _parse_json_object(text):
    """解析 LLM 文本为 dict：整体解析失败时找第一个完整的 {...}；非 dict 返回 None。"""
    text = _strip_code_fence(text)
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    idx = text.find('{')
    while idx != -1:
        try:
            obj, _ = decoder.raw_decode(text, idx)
        except ValueError:
            idx = text.find('{', idx + 1)
            continue
        if isinstance(obj, dict):
            return obj
        idx = text.find('{', idx + 1)
    return None


def _strip_code_fence(text):
    text = text.strip()
    if text.startswith('```'):
        first_newline = text.find('\n')
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.endswith('```'):
            text = text[:-3]
    return text.strip()
