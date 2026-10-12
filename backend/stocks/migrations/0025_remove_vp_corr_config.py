# 清理已删除维度 vp_corr 的配置行（det-v2）。
#
# vp_corr（量价相关性/量能领先）经多轮离线验证无效，注册表与评估代码已删除。
# 它默认停用，scan.load_configs 不会为它建行；但若曾在后台手动启用过，
# DetectorDimConfig 里会留下一行：注册表没有对应条目时它不再被读取，留着只会
# 让管理后台出现一个找不到实现的维度。删除前把名字、权重与参数写进
# DetectorTuneLog（source=legacy）留痕。
# 反向迁移不重建这一行：维度实现已不存在，重建也无法评估。

from django.db import migrations
from django.utils import timezone

KEY = 'vp_corr'


def forwards(apps, schema_editor):
    DetectorDimConfig = apps.get_model('stocks', 'DetectorDimConfig')
    DetectorTuneLog = apps.get_model('stocks', 'DetectorTuneLog')
    for row in DetectorDimConfig.objects.filter(key=KEY):
        DetectorTuneLog.objects.create(
            trade_date=timezone.localdate(),
            dim_key=KEY,
            source='legacy',
            applied=True,
            changes={'removed': [{'name': row.name, 'weight': row.weight, 'params': row.params,
                                  'is_enabled': row.is_enabled}, None]},
            reason='det-v2：vp_corr 经多轮验证无效，删除维度及其配置行',
        )
        row.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('stocks', '0024_detector_annotations_vetoed'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
