"""查询参数解析（ARC-01）：stocks 视图与 detector 视图各有一份手写实现，统一到这里。

两种口径用 strict 区分，默认 strict=False 保留“非法值静默回退默认值”的旧行为：
- strict=False：非法值返回 default（detector/views 的 _positive_int/_parse_date 口径）；
- strict=True：非法值抛 BadRequest（ValueError 子类），由视图转 400（stocks/views 的 _positive_int 口径）。

params 是带 .get(key) 的映射，通常传 request.query_params。
"""

from datetime import datetime

from ..errors import BadRequest

TRUTHY = ('1', 'true', 'yes')
FALSY = ('0', 'false', 'no')


def _empty(raw):
    return raw is None or raw == ''


def positive_int(params, key, default, maximum=None, *, strict=False):
    """正整数参数；缺省或空串返回 default，maximum 为上限（超出时截断，不报错）。"""
    raw = params.get(key)
    if _empty(raw):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        if strict:
            raise BadRequest(f'{key} 必须是正整数')
        return default
    if value < 1:
        if strict:
            raise BadRequest(f'{key} 必须是正整数')
        return default
    return min(value, maximum) if maximum else value


def parse_bool(raw, default=False, *, truthy=TRUTHY, ignore_case=True, strict=False, key='value'):
    """开关参数。缺省或空串返回 default；命中 truthy 为 True。

    strict=False 时其余任何值都是 False（旧的 `x in ('1', 'true')` 写法）；
    strict=True 时只认 truthy/FALSY，其余抛 BadRequest。
    truthy/ignore_case 用于保留各接口原有的取值集合（如 unread 只认小写 '1'/'true'）。
    """
    if _empty(raw):
        return default
    text = str(raw).lower() if ignore_case else str(raw)
    if text in truthy:
        return True
    if strict and text not in FALSY:
        raise BadRequest(f'{key} 必须是布尔值')
    return False


def parse_date(raw, key='date', default=None, *, strict=False):
    """YYYY-MM-DD 日期参数；缺省或空串返回 default。"""
    if _empty(raw):
        return default
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        if strict:
            raise BadRequest(f'{key} 格式应为 YYYY-MM-DD')
        return default
