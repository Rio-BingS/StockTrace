from rest_framework import serializers
from .models import AiProvider
from .ai.crypto import encrypt_api_key, decrypt_api_key, mask_api_key
from .ai.llm import validate_base_url


class AiProviderSerializer(serializers.ModelSerializer):
    # 读：脱敏尾四位；写：明文 key，只写不读
    api_key_masked = serializers.SerializerMethodField()
    api_key = serializers.CharField(write_only=True, required=False, allow_blank=False)

    class Meta:
        model = AiProvider
        fields = [
            'id', 'name', 'base_url', 'model', 'is_enabled',
            'api_key', 'api_key_masked', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_api_key_masked(self, obj):
        try:
            return mask_api_key(decrypt_api_key(obj.api_key_encrypted))
        except ValueError:
            # SECRET_KEY 轮换后旧密文解不开：如实标注而不是假装正常
            return '(无法解密，请重新录入)'

    def validate_base_url(self, value):
        # 只允许 https 公网地址（含 DNS 解析后检查），防 Key 外泄与 SSRF（SEC-01）
        try:
            return validate_base_url(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate(self, attrs):
        api_key = attrs.pop('api_key', None)
        # 改接口地址必须同时重新录入 Key：否则只改 base_url 就能把已存 Key 发往任意地址
        if (
            self.instance is not None
            and 'base_url' in attrs
            and attrs['base_url'] != (self.instance.base_url or '').strip().rstrip('/')
            and not api_key
        ):
            raise serializers.ValidationError({'api_key': '修改接口地址时必须重新录入 API Key'})
        if api_key:
            attrs['api_key_encrypted'] = encrypt_api_key(api_key)
        elif self.instance is None and not attrs.get('api_key_encrypted'):
            raise serializers.ValidationError({'api_key': '创建时必须提供 API Key'})
        return attrs


# ---- AI 选股入参校验（AIX-01）：限制长度与结构，畸形输入返回 400 而不是 500 ----

class ScreenerAiInputSerializer(serializers.Serializer):
    query = serializers.CharField(max_length=200)


class CommentRowSerializer(serializers.Serializer):
    # 只认股票代码；名称与行情由服务端按代码从库里取，不信任客户端传来的数值
    code = serializers.RegexField(r'^\d{6}$', error_messages={'invalid': '股票代码必须是 6 位数字'})


class ScreenerCommentInputSerializer(serializers.Serializer):
    query = serializers.CharField(max_length=200, allow_blank=True, required=False, default='')
    results = serializers.ListField(
        child=CommentRowSerializer(), min_length=1, max_length=50,
        error_messages={
            'min_length': 'results 必须是非空数组',
            'max_length': 'results 最多 50 行',
        },
    )


def first_error_text(errors):
    """把 DRF 嵌套错误压平成一句可直接展示的文字（前端只展示字符串 detail）。"""
    if isinstance(errors, dict):
        for field, value in errors.items():
            text = first_error_text(value)
            if text:
                return text if field == 'non_field_errors' else f'{field}：{text}'
    elif isinstance(errors, (list, tuple)):
        for value in errors:
            text = first_error_text(value)
            if text:
                return text
    elif errors:
        return str(errors)
    return ''
