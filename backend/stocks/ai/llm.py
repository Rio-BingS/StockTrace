"""LLM 客户端：统一走 OpenAI 兼容 /chat/completions 协议。"""
import ipaddress
import json
import socket
from urllib.parse import urlsplit

import requests
from django.conf import settings

from .crypto import decrypt_api_key


class LlmError(Exception):
    """LLM 调用失败（网络/上游/配置）——上层如实报错，绝不伪造结果

    kind：通用错误分类（config/network/timeout/redirect/auth/rate_limit/upstream/bad_response），
    status_code：上游 HTTP 状态码（有则填）。test 端点只回显这两项，不回显上游内容。
    """

    def __init__(self, message, kind='upstream', status_code=None):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code


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

    def chat(self, messages, temperature=0.3, max_tokens=2000, timeout=None):
        """返回 (文本, usage dict)。失败抛 LlmError。"""
        timeout = timeout or int(
            getattr(settings, 'STOCKTRACE_LLM_TIMEOUT', 60)
        )
        # 调用前再次校验：库里可能有旧的不合规地址，且 DNS 解析结果可能已变化
        try:
            validate_base_url(self.base_url)
        except ValueError as exc:
            raise LlmError(f'接口地址不安全：{exc}', kind='config') from exc
        try:
            resp = requests.post(
                f'{self.base_url}/chat/completions',
                headers={
                    'Authorization': f'Bearer {self.api_key}',
                    'Content-Type': 'application/json',
                },
                json={
                    'model': self.provider.model,
                    'messages': messages,
                    'temperature': temperature,
                    'max_tokens': max_tokens,
                },
                timeout=timeout,
                # 不跟随重定向：防止 30x 把带 Key 的请求转到内网或第三方
                allow_redirects=False,
            )
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
            raise LlmError(f'LLM 上游返回 {code}：{resp.text[:300]}',
                           kind=kind, status_code=code)

        try:
            data = resp.json()
            text = data['choices'][0]['message']['content']
        except (ValueError, KeyError, IndexError) as exc:
            raise LlmError(f'LLM 响应格式异常：{resp.text[:300]}',
                           kind='bad_response', status_code=resp.status_code) from exc

        usage = data.get('usage') or {}
        return text, {
            'prompt_tokens': usage.get('prompt_tokens'),
            'completion_tokens': usage.get('completion_tokens'),
        }

    def chat_json(self, messages, temperature=0.0, max_tokens=2000):
        """要求严格 JSON 输出并解析；解析失败抛 LlmError，不猜不修。"""
        text, usage = self.chat(
            messages, temperature=temperature, max_tokens=max_tokens
        )
        try:
            return json.loads(_strip_code_fence(text)), usage
        except ValueError as exc:
            raise LlmError(f'LLM 未返回合法 JSON：{text[:300]}',
                           kind='bad_response') from exc


def _strip_code_fence(text):
    text = text.strip()
    if text.startswith('```'):
        first_newline = text.find('\n')
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.endswith('```'):
            text = text[:-3]
    return text.strip()
