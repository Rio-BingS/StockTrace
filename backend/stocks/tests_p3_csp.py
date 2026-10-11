"""P3 第 14 条：nginx CSP 配置的回归测试（只读 deploy/ 模板，不需要 nginx）。

锁住的点：
- 默认以 Report-Only 上线，强制头的 map 为空（空值 nginx 不下发）；
- 策略包含最小集合与 frame-ancestors/base-uri/object-src/form-action，脚本不放开 unsafe-inline/eval；
- 每个写了 add_header 的块都同时声明两条 CSP（nginx 不继承上层 add_header）；
- 反代 location 隐藏 Django 下发的同名头，响应里不会出现两份；
- 不在 X-Frame-Options 等既有安全头之外重复定义同名头。
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

REPO_DIR = Path(__file__).resolve().parent.parent.parent
CONF = REPO_DIR / 'deploy' / 'nginx-stocktrace.conf'
DOC = REPO_DIR / 'deploy' / 'DEPLOY.md'


def _map_value(conf, var):
    m = re.search(r'map \$host \$%s \{\s*default "([^"]*)";\s*\}' % re.escape(var), conf)
    return None if m is None else m.group(1)


def _blocks(conf):
    """443 server 内的 (名字, 块文本)：server 级（第一个 location 之前）+ 各 location。"""
    server = conf.split('listen 443', 1)[1]
    head, *rest = server.split('\n    location ')
    blocks = [('server', head)]
    for chunk in rest:
        name, body = chunk.split('{', 1)
        blocks.append((name.strip(), body.split('\n    }', 1)[0]))
    return blocks


class NginxCspConfigTests(SimpleTestCase):
    def setUp(self):
        self.conf = CONF.read_text(encoding='utf-8')

    def test_report_only_by_default(self):
        policy = _map_value(self.conf, 'stocktrace_csp_report_only')
        enforce = _map_value(self.conf, 'stocktrace_csp')
        self.assertTrue(policy, '观察期策略应放在 Report-Only 的 map 里')
        self.assertEqual(enforce, '', '强制 CSP 的 map 默认必须为空，观察后再切换')

    def test_policy_directives(self):
        policy = _map_value(self.conf, 'stocktrace_csp_report_only')
        directives = {d.split()[0]: d.split()[1:] for d in policy.split(';') if d.strip()}
        self.assertEqual(directives['default-src'], ["'self'"])
        self.assertEqual(directives['script-src'], ["'self'"])
        # ECharts tooltip 以 innerHTML 写入 style 属性，必须保留
        self.assertEqual(directives['style-src'], ["'self'", "'unsafe-inline'"])
        self.assertEqual(directives['connect-src'], ["'self'"])
        self.assertEqual(directives['object-src'], ["'none'"])
        self.assertEqual(directives['frame-ancestors'], ["'none'"])
        self.assertEqual(directives['form-action'], ["'self'"])
        self.assertIn('base-uri', directives)
        self.assertNotIn("'unsafe-eval'", policy)
        self.assertNotIn('*', policy)
        self.assertNotIn('\n', policy)

    def test_every_add_header_block_declares_both_csp(self):
        for name, body in _blocks(self.conf):
            if 'add_header' not in body:
                continue
            with self.subTest(block=name):
                self.assertIn('add_header Content-Security-Policy $stocktrace_csp always;', body)
                self.assertIn(
                    'add_header Content-Security-Policy-Report-Only $stocktrace_csp_report_only always;',
                    body)

    def test_no_duplicate_headers_within_block(self):
        for name, body in _blocks(self.conf):
            headers = re.findall(r'^\s*add_header\s+(\S+)', body, flags=re.M)
            with self.subTest(block=name):
                self.assertEqual(len(headers), len(set(h.lower() for h in headers)), headers)

    def test_proxied_locations_hide_upstream_csp(self):
        for name, body in _blocks(self.conf):
            if 'proxy_pass' not in body:
                continue
            with self.subTest(block=name):
                self.assertIn('proxy_hide_header Content-Security-Policy;', body)
                self.assertIn('proxy_hide_header Content-Security-Policy-Report-Only;', body)
                self.assertIn('proxy_hide_header X-Frame-Options;', body)

    def test_deploy_doc_has_enforce_steps(self):
        doc = DOC.read_text(encoding='utf-8')
        self.assertIn('Content-Security-Policy-Report-Only', doc)
        self.assertIn('nginx -t', doc)
        self.assertIn('stocktrace_csp', doc)
