"""条件选股预设。"""

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response


@api_view(['GET', 'POST'])
def screener_preset_list(request):
    """预设列表 / 保存（spec 用与执行完全相同的校验规则，保存即可执行）"""
    from ..screener.engine import ConditionError, _validate
    from ..models import ScreenerPreset

    if request.method == 'GET':
        from ..screener.strategies import BUILTIN_STRATEGIES

        presets = ScreenerPreset.objects.all()
        builtins = [{
            'id': None,
            'builtin': True,
            'name': s['name'],
            'desc': s.get('desc', ''),
            'spec': s['spec'],
            'created_at': None,
        } for s in BUILTIN_STRATEGIES]
        return Response(builtins + [{
            'id': preset.id,
            'builtin': False,
            'name': preset.name,
            'spec': preset.spec,
            'created_at': preset.created_at,
        } for preset in presets])

    name = (request.data.get('name') or '').strip()
    spec = request.data.get('spec')
    if not name or len(name) > 50:
        return Response({'detail': '预设名称必填且不超过 50 字'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        _validate(spec)
    except ConditionError as e:
        return Response({'detail': f'筛选条件不合法：{e}'}, status=status.HTTP_400_BAD_REQUEST)
    if ScreenerPreset.objects.filter(name=name).exists():
        return Response({'detail': f'预设「{name}」已存在'}, status=status.HTTP_409_CONFLICT)
    try:
        with transaction.atomic():
            preset = ScreenerPreset.objects.create(name=name, spec=spec)
    except IntegrityError:  # 并发同名创建（PERF-06）
        return Response({'detail': f'预设「{name}」已存在'}, status=status.HTTP_409_CONFLICT)
    return Response({'id': preset.id, 'name': preset.name, 'spec': preset.spec},
                    status=status.HTTP_201_CREATED)


@api_view(['DELETE'])
def screener_preset_detail(request, pk):
    from ..models import ScreenerPreset

    preset = ScreenerPreset.objects.filter(id=pk).first()
    if preset is None:
        return Response({'detail': '预设不存在'}, status=status.HTTP_404_NOT_FOUND)
    preset.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)
