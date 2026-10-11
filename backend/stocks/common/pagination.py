"""列表分页（板块、ETF 雷达、探测器信号共用，ARC-01）。"""

from __future__ import annotations


def paginate(items, page, page_size):
    """按页切片，返回 (当页条目, 分页信息)。

    page 超出总页数时钳到最后一页；空列表也视为 1 页。分页信息字段：
    page / page_size / total / total_pages。
    """
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    return items[start:start + page_size], {
        'page': page,
        'page_size': page_size,
        'total': total,
        'total_pages': total_pages,
    }
