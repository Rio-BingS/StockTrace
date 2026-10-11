"""P1 安全与配置回归测试（SEC-02~06、OPS-05/06）"""
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings

from .ai import crypto
from .models import AiProvider

BACKEND_DIR = Path(settings.BASE_DIR)
_GOOD_SECRET = 'x' * 64


def _load_settings(env_overrides):
    """在子进程里用给定环境变量导入 settings，返回 (returncode, stderr)"""
    env = {k: v for k, v in os.environ.items() if not k.startswith(('DJANGO_', 'STOCKTRACE_'))}
    env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, '-c', 'import StockTrace.settings as s; print(s.SECRET_KEY[:4])'],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True, timeout=60,
    )
    return proc.returncode, proc.stderr


def _settings_values(env_overrides, *names):
    """在子进程里导入 settings，返回指定变量的 repr 字典"""
    env = {k: v for k, v in os.environ.items() if not k.startswith(('DJANGO_', 'STOCKTRACE_'))}
    env.setdefault('DJANGO_SECRET_KEY', _GOOD_SECRET)
    env.update(env_overrides)
    code = 'import json, StockTrace.settings as s; print(json.dumps({n: getattr(s, n) for n in %r}))' % (
        list(names),
    )
    proc = subprocess.run(
        [sys.executable, '-c', code], cwd=BACKEND_DIR, env=env,
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    import json
    return json.loads(proc.stdout.strip().splitlines()[-1])


class SecretKeyGuardTests(TestCase):
    """SEC-04：生产拒绝占位符/过短 SECRET_KEY"""

    def test_placeholder_rejected_in_production(self):
        code, err = _load_settings({
            'DJANGO_DEBUG': 'False',
            'DJANGO_SECRET_KEY': 'change-me-to-a-long-random-string',
        })
        self.assertNotEqual(code, 0)
        self.assertIn('ImproperlyConfigured', err)

    def test_short_key_rejected_in_production(self):
        code, err = _load_settings({'DJANGO_DEBUG': 'False', 'DJANGO_SECRET_KEY': 'short-key'})
        self.assertNotEqual(code, 0)
        self.assertIn('ImproperlyConfigured', err)

    def test_long_random_key_accepted(self):
        code, err = _load_settings({'DJANGO_DEBUG': 'False', 'DJANGO_SECRET_KEY': _GOOD_SECRET})
        self.assertEqual(code, 0, err)

    def test_debug_allows_short_dev_key(self):
        code, err = _load_settings({'DJANGO_DEBUG': 'True', 'DJANGO_SECRET_KEY': 'test'})
        self.assertEqual(code, 0, err)

    def test_invalid_fernet_key_rejected(self):
        code, err = _load_settings({
            'DJANGO_DEBUG': 'False',
            'DJANGO_SECRET_KEY': _GOOD_SECRET,
            'STOCKTRACE_FERNET_KEY': 'not-a-fernet-key',
        })
        self.assertNotEqual(code, 0)
        self.assertIn('STOCKTRACE_FERNET_KEY', err)


class FernetKeyTests(TestCase):
    """SEC-04：API Key 加密密钥与 SECRET_KEY 解耦，兼容旧密文并支持轮换"""

    def setUp(self):
        self.k1 = Fernet.generate_key().decode()
        self.k2 = Fernet.generate_key().decode()

    def test_legacy_token_still_decrypts_after_configuring_new_key(self):
        with override_settings(STOCKTRACE_FERNET_KEYS=[]):
            legacy_token = crypto.encrypt_api_key('sk-legacy-0001')
        with override_settings(STOCKTRACE_FERNET_KEYS=[self.k1]):
            self.assertEqual(crypto.decrypt_api_key(legacy_token), 'sk-legacy-0001')

    def test_new_key_encrypts_independent_of_secret_key(self):
        with override_settings(STOCKTRACE_FERNET_KEYS=[self.k1]):
            token = crypto.encrypt_api_key('sk-new-0002')
        # 只用独立密钥即可解开，与 SECRET_KEY 无关
        self.assertEqual(Fernet(self.k1).decrypt(token.encode()).decode(), 'sk-new-0002')
        with override_settings(STOCKTRACE_FERNET_KEYS=[self.k1], SECRET_KEY='y' * 64):
            self.assertEqual(crypto.decrypt_api_key(token), 'sk-new-0002')

    def test_old_fernet_key_kept_for_decrypt_during_rotation(self):
        with override_settings(STOCKTRACE_FERNET_KEYS=[self.k1]):
            token = crypto.encrypt_api_key('sk-rot-0003')
        with override_settings(STOCKTRACE_FERNET_KEYS=[self.k2, self.k1]):
            self.assertEqual(crypto.decrypt_api_key(token), 'sk-rot-0003')
        with override_settings(STOCKTRACE_FERNET_KEYS=[self.k2]):
            with self.assertRaises(ValueError):
                crypto.decrypt_api_key(token)

    def _provider(self, token):
        return AiProvider.objects.create(
            name='p', base_url='https://api.example.com/v1', api_key_encrypted=token, model='m',
        )

    def test_rotate_command_reencrypts_with_primary_key(self):
        with override_settings(STOCKTRACE_FERNET_KEYS=[]):
            p = self._provider(crypto.encrypt_api_key('sk-cmd-0004'))
        with override_settings(STOCKTRACE_FERNET_KEYS=[self.k1]):
            out = StringIO()
            call_command('rotate_api_keys', stdout=out)
            self.assertIn('已重加密 1 条', out.getvalue())
        p.refresh_from_db()
        self.assertEqual(Fernet(self.k1).decrypt(p.api_key_encrypted.encode()).decode(), 'sk-cmd-0004')
        self.assertNotIn('sk-cmd-0004', out.getvalue())
        # 换掉 SECRET_KEY 后依旧能解
        with override_settings(STOCKTRACE_FERNET_KEYS=[self.k1], SECRET_KEY='z' * 64):
            self.assertEqual(crypto.decrypt_api_key(p.api_key_encrypted), 'sk-cmd-0004')

    def test_rotate_command_dry_run_does_not_write(self):
        with override_settings(STOCKTRACE_FERNET_KEYS=[]):
            token = crypto.encrypt_api_key('sk-dry-0005')
            p = self._provider(token)
        with override_settings(STOCKTRACE_FERNET_KEYS=[self.k1]):
            call_command('rotate_api_keys', '--dry-run', stdout=StringIO())
        p.refresh_from_db()
        self.assertEqual(p.api_key_encrypted, token)

    def test_rotate_command_refuses_without_independent_key(self):
        with override_settings(STOCKTRACE_FERNET_KEYS=[]):
            with self.assertRaises(CommandError):
                call_command('rotate_api_keys', stdout=StringIO())

    def test_rotate_command_rolls_back_when_undecryptable(self):
        with override_settings(STOCKTRACE_FERNET_KEYS=[]):
            good = self._provider(crypto.encrypt_api_key('sk-good-0006'))
        good_token = good.api_key_encrypted
        self._provider(Fernet(Fernet.generate_key()).encrypt(b'sk-lost').decode())
        with override_settings(STOCKTRACE_FERNET_KEYS=[self.k1]):
            with self.assertRaises(CommandError):
                call_command('rotate_api_keys', stdout=StringIO())
        good.refresh_from_db()
        self.assertEqual(good.api_key_encrypted, good_token)


_HOST = 'stocktrace.yumeat.cc.cd'
_ORIGIN = 'https://stocktrace.yumeat.cc.cd'


class HttpsProxySettingsTests(TestCase):
    """SEC-05：HTTPS 反代下的受信来源 / Secure Cookie / HSTS 配置"""

    def test_production_defaults(self):
        v = _settings_values(
            {'DJANGO_DEBUG': 'False'},
            'CORS_ALLOWED_ORIGINS', 'CSRF_TRUSTED_ORIGINS', 'SECURE_PROXY_SSL_HEADER',
            'SESSION_COOKIE_SECURE', 'CSRF_COOKIE_SECURE',
            'SECURE_HSTS_SECONDS', 'SECURE_HSTS_INCLUDE_SUBDOMAINS',
        )
        self.assertEqual(v['CORS_ALLOWED_ORIGINS'], [_ORIGIN])
        self.assertEqual(v['CSRF_TRUSTED_ORIGINS'], [_ORIGIN])
        self.assertEqual(v['SECURE_PROXY_SSL_HEADER'], ['HTTP_X_FORWARDED_PROTO', 'https'])
        self.assertTrue(v['SESSION_COOKIE_SECURE'])
        self.assertTrue(v['CSRF_COOKIE_SECURE'])
        self.assertEqual(v['SECURE_HSTS_SECONDS'], 0)
        self.assertFalse(v['SECURE_HSTS_INCLUDE_SUBDOMAINS'])

    def test_no_http_origin_by_default(self):
        v = _settings_values({'DJANGO_DEBUG': 'False'}, 'CORS_ALLOWED_ORIGINS')
        self.assertFalse([o for o in v['CORS_ALLOWED_ORIGINS'] if o.startswith('http:')])

    def test_overrides(self):
        v = _settings_values(
            {
                'DJANGO_DEBUG': 'False',
                'STOCKTRACE_TRUSTED_ORIGINS': 'https://a.example.com/, https://b.example.com',
                'STOCKTRACE_SECURE_COOKIES': '0',
                'STOCKTRACE_HSTS_SECONDS': '31536000',
                'STOCKTRACE_HSTS_INCLUDE_SUBDOMAINS': '1',
            },
            'CORS_ALLOWED_ORIGINS', 'CSRF_TRUSTED_ORIGINS', 'SESSION_COOKIE_SECURE',
            'SECURE_HSTS_SECONDS', 'SECURE_HSTS_INCLUDE_SUBDOMAINS',
        )
        expected = ['https://a.example.com', 'https://b.example.com']
        self.assertEqual(v['CORS_ALLOWED_ORIGINS'], expected)
        self.assertEqual(v['CSRF_TRUSTED_ORIGINS'], expected)
        self.assertFalse(v['SESSION_COOKIE_SECURE'])
        self.assertEqual(v['SECURE_HSTS_SECONDS'], 31536000)
        self.assertTrue(v['SECURE_HSTS_INCLUDE_SUBDOMAINS'])

    def test_debug_defaults_to_insecure_cookies(self):
        v = _settings_values({'DJANGO_DEBUG': 'True'}, 'SESSION_COOKIE_SECURE')
        self.assertFalse(v['SESSION_COOKIE_SECURE'])


@override_settings(
    ALLOWED_HOSTS=[_HOST], CSRF_TRUSTED_ORIGINS=[_ORIGIN],
    SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True,
)
class AdminLoginBehindHttpsProxyTests(TestCase):
    """SEC-05：Nginx 终止 TLS 后转发 http + X-Forwarded-Proto，admin 登录应 302 而非 403"""

    def setUp(self):
        get_user_model().objects.create_superuser('admin', 'a@example.com', 'pw-Very-Long-123')
        self.client = Client(enforce_csrf_checks=True)
        self.proxy = {
            'HTTP_HOST': _HOST, 'HTTP_X_FORWARDED_PROTO': 'https', 'HTTP_ORIGIN': _ORIGIN,
            'HTTP_REFERER': _ORIGIN + '/admin/login/',
        }

    def _login(self, **extra):
        resp = self.client.get('/admin/login/', **{**self.proxy, **extra})
        self.assertEqual(resp.status_code, 200)
        token = resp.cookies['csrftoken'].value
        self.assertTrue(resp.cookies['csrftoken']['secure'])
        return self.client.post(
            '/admin/login/?next=/admin/',
            {'username': 'admin', 'password': 'pw-Very-Long-123', 'csrfmiddlewaretoken': token},
            **{**self.proxy, **extra},
        )

    def test_admin_login_redirects_behind_https_proxy(self):
        resp = self._login()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], '/admin/')
        self.assertTrue(resp.cookies['sessionid']['secure'])

    def test_untrusted_origin_still_rejected(self):
        resp = self._login(HTTP_ORIGIN='https://evil.example.com')
        self.assertEqual(resp.status_code, 403)
