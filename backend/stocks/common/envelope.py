"""列表接口的统一响应外壳 {items, pagination, meta, ...}（ARC-04）。

- 步骤 4a：本来就是这种形状的接口（板块轮动、ETF 雷达、探测器信号）用 envelope()
  组装，传 key_order 保持历史键顺序，输出逐字节不变。
- 步骤 4c/4d：裸数组接口和自选列表（DRF 分页）用 ?envelope=1 过渡一个版本：
  带参数返回外壳，不带参数保持旧输出逐字节不变（tests_p3_envelope 快照锁定）。
  默认值由 settings.STOCKTRACE_ENVELOPE_DEFAULT 控制（缺省 False），
  下一版翻转为 True 时旧客户端可用 ?envelope=0 再兼容一版。

外壳约定：items 是当前页（或全量）条目；pagination 为 null 表示不分页、
items 即全量（有截断时看 meta.truncated），否则为
{page, page_size, total, total_pages}（与 common.pagination.paginate 同字段）；
meta 总是对象，放与条目无关的上下文（查询条件、截断标记等）。
"""

from django.conf import settings
from django.core.paginator import Paginator
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from .params import parse_bool

ENVELOPE_KEYS = ('items', 'pagination', 'meta')
ENVELOPE_PARAM = 'envelope'


def envelope(items, pagination=None, meta=None, *, key_order=None, **extra):
    """组装 {items, pagination, meta, **extra}。

    新接口不传 key_order，键顺序为 items、pagination、meta、extra。
    已上线接口传 key_order（全部键的历史顺序），保证 JSON 键顺序与旧实现一致；
    key_order 必须恰好覆盖全部键，漏写或多写直接报错，避免悄悄丢字段。
    """
    body = {'items': items, 'pagination': pagination, 'meta': meta, **extra}
    if key_order is None:
        return body
    key_order = tuple(key_order)
    if len(key_order) != len(set(key_order)) or set(key_order) != set(body):
        raise ValueError(f'envelope order 与字段不一致：{sorted(set(key_order) ^ set(body))}')
    return {key: body[key] for key in key_order}


def wants_envelope(request):
    """本次请求是否要外壳：?envelope=1/true/yes 要，0/false/其他值不要。

    参数缺省或为空串时取 settings.STOCKTRACE_ENVELOPE_DEFAULT（本版缺省 False）。
    """
    default = bool(getattr(settings, 'STOCKTRACE_ENVELOPE_DEFAULT', False))
    return parse_bool(request.query_params.get(ENVELOPE_PARAM), default=default)


def list_response(request, items, *, pagination=None, meta=None):
    """裸数组列表接口的过渡出口：要外壳时包成 {items, pagination, meta}，否则原样返回数组。"""
    if wants_envelope(request):
        return Response(envelope(items, pagination, meta if meta is not None else {}))
    return Response(items)


class EnvelopePagination(PageNumberPagination):
    """自选列表等 DRF 列表的外壳分页（ARC-04 步骤 4d）。

    与全局 PageNumberPagination 的区别：
    - 输出 {items, pagination, meta}，pagination 字段与 common.pagination.paginate 一致；
    - 支持 ?page_size=N（上限 max_page_size），便于前端一次取全量自选；
    - page 非法回到第 1 页、超出总页数钳到最后一页（与板块/ETF/探测器分页口径一致），
      不像 DRF 默认那样返回 404。
    """

    page_size_query_param = 'page_size'
    max_page_size = 1000

    def paginate_queryset(self, queryset, request, view=None):
        page_size = self.get_page_size(request)
        if not page_size:
            return None
        self.request = request
        self.page = Paginator(queryset, page_size).get_page(request.query_params.get(self.page_query_param))
        return list(self.page)

    def get_pagination(self):
        paginator = self.page.paginator
        return {
            'page': self.page.number,
            'page_size': paginator.per_page,
            'total': paginator.count,
            'total_pages': paginator.num_pages,
        }

    def get_paginated_response(self, data):
        return Response(envelope(data, self.get_pagination(), {}))

    def get_paginated_response_schema(self, schema):
        return {
            'type': 'object',
            'required': list(ENVELOPE_KEYS),
            'properties': {
                'items': schema,
                'pagination': {
                    'type': 'object',
                    'properties': {k: {'type': 'integer'} for k in ('page', 'page_size', 'total', 'total_pages')},
                },
                'meta': {'type': 'object'},
            },
        }
