"""自选分组。"""

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response


def _group_row(group):
    # 列表查询已用 annotate 算好活跃股票数（PERF-08：旧实现每组一次 count()）；
    # 单个分组的新建/修改路径没有注解，退回单次计数
    count = getattr(group, 'active_stock_count', None)
    if count is None:
        count = group.stocks.filter(is_active=True).count()
    return {
        'id': group.id,
        'name': group.name,
        'order': group.order,
        'stock_count': count,
        'created_at': group.created_at,
    }


@api_view(['GET', 'POST'])
def stock_group_list(request):
    """分组列表（含各组活跃股票数）/ 新建"""
    from ..models import StockGroup

    if request.method == 'GET':
        from django.db.models import Count, Q

        groups = StockGroup.objects.annotate(
            active_stock_count=Count('stocks', filter=Q(stocks__is_active=True)),
        )
        return Response([_group_row(g) for g in groups])

    name = (request.data.get('name') or '').strip()
    if not name or len(name) > 50:
        return Response({'detail': '分组名称必填且不超过 50 字'}, status=status.HTTP_400_BAD_REQUEST)
    if StockGroup.objects.filter(name=name).exists():
        return Response({'detail': f'分组「{name}」已存在'}, status=status.HTTP_409_CONFLICT)
    try:
        with transaction.atomic():
            group = StockGroup.objects.create(name=name)
    except IntegrityError:  # 并发同名创建（PERF-06）
        return Response({'detail': f'分组「{name}」已存在'}, status=status.HTTP_409_CONFLICT)
    return Response(_group_row(group), status=status.HTTP_201_CREATED)


@api_view(['PATCH', 'DELETE'])
def stock_group_detail(request, pk):
    """重命名/调序（PATCH {name?, order?}）；删除分组后组内股票变回未分组"""
    from ..models import StockGroup

    group = StockGroup.objects.filter(id=pk).first()
    if group is None:
        return Response({'detail': '分组不存在'}, status=status.HTTP_404_NOT_FOUND)
    if request.method == 'DELETE':
        group.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    name = request.data.get('name')
    if name is not None:
        name = str(name).strip()
        if not name or len(name) > 50:
            return Response({'detail': '分组名称不能为空且不超过 50 字'}, status=status.HTTP_400_BAD_REQUEST)
        if StockGroup.objects.filter(name=name).exclude(id=group.id).exists():
            return Response({'detail': f'分组「{name}」已存在'}, status=status.HTTP_409_CONFLICT)
        group.name = name
    if 'order' in request.data:
        try:
            group.order = int(request.data['order'])
        except (TypeError, ValueError):
            return Response({'detail': 'order 必须是整数'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        with transaction.atomic():
            group.save()
    except IntegrityError:  # 并发重命名撞名（PERF-06）
        return Response({'detail': f'分组「{name}」已存在'}, status=status.HTTP_409_CONFLICT)
    return Response(_group_row(group))
