"""API Key 加密存储：Fernet 对称加密（SEC-04）。

加密密钥优先取 settings.STOCKTRACE_FERNET_KEYS（环境变量 STOCKTRACE_FERNET_KEY，
逗号分隔，第一个加密、其余只解密），与 SECRET_KEY 解耦，可独立轮换；
未配置时回退为由 DJANGO_SECRET_KEY 派生的旧密钥并记 WARNING。
旧派生密钥始终参与解密，所以切换到独立密钥后旧密文仍可读；
执行 `python manage.py rotate_api_keys` 把全部密文改用当前主密钥重加密后，
SECRET_KEY 才可以独立轮换。
"""
import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

_warned_legacy = False


def _legacy_key() -> bytes:
    """旧方案：sha256(SECRET_KEY) 派生的 Fernet key（仅为兼容旧密文保留）"""
    secret = settings.SECRET_KEY
    if not secret:
        raise ImproperlyConfigured('SECRET_KEY 未设置，无法加解密 API Key')
    digest = hashlib.sha256(secret.encode('utf-8')).digest()
    return base64.urlsafe_b64encode(digest)


def uses_legacy_key() -> bool:
    """是否仍在用 SECRET_KEY 派生密钥加密（未配置 STOCKTRACE_FERNET_KEY）"""
    return not list(getattr(settings, 'STOCKTRACE_FERNET_KEYS', []) or [])


def _fernet() -> MultiFernet:
    global _warned_legacy
    keys = [k.encode('ascii') for k in (getattr(settings, 'STOCKTRACE_FERNET_KEYS', []) or [])]
    legacy = _legacy_key()
    if not keys and not _warned_legacy:
        _warned_legacy = True
        logger.warning(
            '未配置 STOCKTRACE_FERNET_KEY，API Key 仍用 SECRET_KEY 派生密钥加密；'
            '建议配置独立密钥并执行 manage.py rotate_api_keys'
        )
    if legacy not in keys:
        keys.append(legacy)  # 旧密文兼容：始终可用派生密钥解密
    return MultiFernet([Fernet(k) for k in keys])


def encrypt_api_key(plain: str) -> str:
    return _fernet().encrypt(plain.encode('utf-8')).decode('ascii')


def decrypt_api_key(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode('ascii')).decode('utf-8')
    except InvalidToken as exc:
        raise ValueError(
            'API Key 解密失败（加密密钥或 SECRET_KEY 可能已轮换，请重新录入）'
        ) from exc


def rotate_api_key(token: str) -> str:
    """用当前主密钥重加密密文（任一已配置密钥或旧派生密钥能解开即可）"""
    try:
        return _fernet().rotate(token.encode('ascii')).decode('ascii')
    except InvalidToken as exc:
        raise ValueError('API Key 解密失败，无法重加密') from exc


def mask_api_key(plain: str) -> str:
    """脱敏展示：只回尾四位"""
    if len(plain) <= 4:
        return '****'
    return '****' + plain[-4:]
