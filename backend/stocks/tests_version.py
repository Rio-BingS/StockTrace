"""应用版本号：唯一来源为仓库根目录 VERSION，经 settings.APP_VERSION 暴露到 /api/health/。"""

import os
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import TestCase

VERSION_FILE = Path(settings.BASE_DIR).parent / 'VERSION'


class AppVersionTests(TestCase):
    def test_version_file_is_semver(self):
        text = VERSION_FILE.read_text(encoding='utf-8').strip()
        parts = text.split('.')
        self.assertEqual(len(parts), 3, text)
        self.assertTrue(all(p.isdigit() for p in parts), text)

    def test_settings_reads_version_file(self):
        self.assertEqual(settings.APP_VERSION, VERSION_FILE.read_text(encoding='utf-8').strip())

    def test_health_reports_version(self):
        with mock.patch.dict(os.environ, {'STOCKTRACE_SCHEDULER': '0'}):
            r = self.client.get('/api/health/')
        self.assertEqual(r.json()['version'], settings.APP_VERSION)
