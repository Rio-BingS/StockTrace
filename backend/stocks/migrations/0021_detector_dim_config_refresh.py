# 探测器维度配置行对齐新注册表（集成 data-align #6 #9、thresholds #1 #3 #10 #13）。
#
# 为什么要数据迁移：scan.load_configs 用库里的 name 展示，并以
# {**注册表默认, **库里 params} 合并参数，而旧配置行存的是当初完整的默认参数：
# - 改名的维度仍显示旧名（大单净流入、融资余额/北向）；
# - 旧 noise_window=60 会覆盖新默认 120（上下文拉长后噪声窗口拉不满），价格类
#   维度残留的 min/max/max_dist 等键虽已失效，仍会出现在配置页与调参计算里。
# 处理：名字改成注册表新名；6 个启用维度的 params 重置为本次的新默认值，权重保留。
# 旧的名字与参数逐行写进 DetectorTuneLog（source=legacy）留痕，回滚时据此还原。
# 默认值按迁移当时的注册表写死（迁移是历史快照，不随以后的注册表变动）。

from django.db import migrations
from django.utils import timezone

_NOISE = {'noise_pct': 0.8, 'noise_window': 120, 'noise_min': 15, 'noise_hi': 2.5}

NEW_NAMES = {
    'big_order_net': '板块资金净额',
    'margin_north': '融资余额',
}

NEW_PARAMS = {
    'big_order_net': {'window': 15, 'mean_days': 6, 'mild': 0.5, 'min': 0.08, 'max': 2.0, **_NOISE},
    'vol_ratio_slope': {'window': 10, 'slope_days': 3, 'mild': 0.15, 'min': 0.02, 'max': 0.5, **_NOISE},
    'vol_compress': {'window': 5, 'compress_pct': 0.25, 'z_lo': 1.645, 'z_hi': 3.0, 'z_mild': 2.0,
                     'sigma_window': 60, 'sigma_min': 20, 'noise_window': 120, 'noise_min': 15},
    'turnover_pct': {'window': 60, 'delta_days': 5, 'mild': 30.0, 'min': 8.0, 'max': 60.0,
                     'pos_cap': 70.0, 'pos_floor': 30.0, 'split_ratio': 1.2, **_NOISE},
    'margin_north': {'mild': 0.5, 'min': 0.06, 'max': 2.0, **_NOISE},
    'ma_relation': {'ma': 20, 'z_lo': 1.282, 'z_hi': 2.0, 'z_mild': 2.0,
                    'sigma_window': 60, 'sigma_min': 20},
}

REASON = '0021 迁移：维度名与参数对齐新注册表（权重保留）'


def refresh_configs(apps, schema_editor):
    DetectorDimConfig = apps.get_model('stocks', 'DetectorDimConfig')
    DetectorTuneLog = apps.get_model('stocks', 'DetectorTuneLog')
    today = timezone.localdate()
    for cfg in DetectorDimConfig.objects.filter(key__in=list(NEW_PARAMS)):
        changes = {}
        new_name = NEW_NAMES.get(cfg.key)
        if new_name and cfg.name != new_name:
            changes['name'] = [cfg.name, new_name]
            cfg.name = new_name
        new_params = NEW_PARAMS[cfg.key]
        if (cfg.params or {}) != new_params:
            changes['params'] = [cfg.params or {}, new_params]
            cfg.params = dict(new_params)
        if not changes:
            continue
        cfg.save(update_fields=['name', 'params', 'updated_at'])
        DetectorTuneLog.objects.create(
            trade_date=today, dim_key=cfg.key, source='legacy', applied=True,
            changes=changes, stats={'weight': cfg.weight}, reason=REASON,
        )


def restore_configs(apps, schema_editor):
    """回滚：按留痕把名字与参数还原成迁移前的值，并删掉这些留痕行。"""
    DetectorDimConfig = apps.get_model('stocks', 'DetectorDimConfig')
    DetectorTuneLog = apps.get_model('stocks', 'DetectorTuneLog')
    logs = DetectorTuneLog.objects.filter(source='legacy', reason=REASON).order_by('-id')
    for log in logs:
        cfg = DetectorDimConfig.objects.filter(key=log.dim_key).first()
        if cfg is None:
            continue
        changes = log.changes or {}
        if 'name' in changes:
            cfg.name = changes['name'][0]
        if 'params' in changes:
            cfg.params = changes['params'][0]
        cfg.save(update_fields=['name', 'params', 'updated_at'])
    logs.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('stocks', '0020_detectortunelog'),
    ]

    operations = [
        migrations.RunPython(refresh_configs, restore_configs),
    ]
