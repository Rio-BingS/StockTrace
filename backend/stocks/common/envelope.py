"""列表接口的统一响应外壳 {items, pagination, meta, ...}（ARC-04 步骤 4a）。

当前只用于本来就是这种形状的接口（板块轮动、ETF 雷达），输出逐字节不变；
裸数组 / DRF 分页接口迁到外壳是破坏性变更（步骤 4c/4d），需前端配合，另行排期。
"""

ENVELOPE_KEYS = ('items', 'pagination', 'meta')


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
