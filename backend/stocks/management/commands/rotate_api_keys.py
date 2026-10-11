"""把 AI 服务商的已存 API Key 改用当前主加密密钥重加密（SEC-04）。

为什么提供命令：加密密钥已与 SECRET_KEY 解耦（STOCKTRACE_FERNET_KEY），
但存量密文仍是旧密钥加的；不重加密的话，轮换 SECRET_KEY 或移除旧 Fernet key
后这些密文就解不开了。命令只在内存中解密，不输出明文。

用法（先加载 /etc/stocktrace/env，并先做一次数据库备份）：
    python manage.py rotate_api_keys --dry-run   # 只检查能否全部解开
    python manage.py rotate_api_keys
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = '用 STOCKTRACE_FERNET_KEY 的第一个密钥重加密全部已存 API Key（不输出明文）'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='只检查能否解密，不写库')

    def handle(self, *args, **options):
        from stocks.ai.crypto import rotate_api_key, uses_legacy_key
        from stocks.models import AiProvider

        if uses_legacy_key():
            raise CommandError(
                '未配置 STOCKTRACE_FERNET_KEY：重加密后仍依赖 SECRET_KEY，没有意义。'
                '请先在 /etc/stocktrace/env 配置独立密钥并重新加载环境变量'
            )

        failed = []
        rotated = 0
        with transaction.atomic():
            for p in AiProvider.objects.select_for_update().order_by('id'):
                try:
                    token = rotate_api_key(p.api_key_encrypted)
                except ValueError:
                    failed.append(f'{p.id}:{p.name}')
                    continue
                if not options['dry_run']:
                    AiProvider.objects.filter(pk=p.pk).update(api_key_encrypted=token)
                rotated += 1
            if failed:
                # 有解不开的就整体回滚，避免一半新一半旧难以排查
                raise CommandError(
                    f'{len(failed)} 条无法解密（未改动任何数据）：{", ".join(failed)}；'
                    '可在设置页重新录入这些 Key 后重试'
                )

        verb = '可重加密' if options['dry_run'] else '已重加密'
        self.stdout.write(self.style.SUCCESS(f'{verb} {rotated} 条 API Key'))
