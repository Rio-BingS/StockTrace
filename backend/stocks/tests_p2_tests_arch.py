"""P2 tests-arch 方向回归：主键类型钉住（ARC-08）、交易日历独立模块（ARC-02）。"""

from django.apps import apps
from django.conf import settings
from django.db import models
from django.test import SimpleTestCase


class DefaultAutoFieldTests(SimpleTestCase):
    """ARC-08：DEFAULT_AUTO_FIELD 显式为 BigAutoField，与现有迁移一致。"""

    def test_setting_is_big_auto_field(self):
        self.assertEqual(settings.DEFAULT_AUTO_FIELD, 'django.db.models.BigAutoField')

    def test_all_stocks_models_use_big_auto_field(self):
        for model in apps.get_app_config('stocks').get_models():
            self.assertIsInstance(model._meta.pk, models.BigAutoField, model.__name__)
