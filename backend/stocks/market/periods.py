"""行情区间统一解析：预设档位 + 自定义起止。

档位语义（自然日回推，end 默认今天）：
    1d 当日 / 3d 近3日 / 5d 近5日 / 1w 近1周 / 2w 近2周
    1m 近1月 / 3m 近3月 / 6m 近6月 / 1y 近1年 / ytd 今年以来

每个数据接口通过 `allowed` 声明自己支持的档位（如当日横截面数据只有
'1d'），通过 allow_custom 控制是否支持自定义起止；超出上游数据深度时
由各接口在返回的 meta 中如实标注覆盖范围。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from django.utils import timezone

PERIOD_LABELS = {
    '1d': '当日',
    '3d': '近3日',
    '5d': '近5日',
    '1w': '近1周',
    '2w': '近2周',
    '1m': '近1月',
    '3m': '近3月',
    '6m': '近6月',
    '1y': '近1年',
    'ytd': '今年以来',
    'custom': '自定义',
}

_PRESET_DAYS = {
    '1d': 1, '3d': 3, '5d': 5, '1w': 7, '2w': 14,
    '1m': 30, '3m': 90, '6m': 182, '1y': 365,
}

# 日频历史型数据的全档位（受上游深度限制时由接口标注覆盖范围）
FULL_PRESETS = ['1d', '3d', '5d', '1w', '1m', '3m', '6m', '1y', 'ytd']
# 当日横截面型数据（上游无历史，多周期需快照积累）
DAILY_ONLY = ['1d']


class PeriodWindow:
    """解析后的区间窗口。"""

    def __init__(self, preset, start, end):
        self.preset = preset
        self.start = start
        self.end = end

    def meta(self):
        return {
            'preset': self.preset,
            'label': PERIOD_LABELS.get(self.preset, self.preset),
            'start': self.start.isoformat(),
            'end': self.end.isoformat(),
        }

    def contains(self, date_str):
        """ISO 日期字符串是否落在窗口内。"""
        return bool(date_str) and self.start.isoformat() <= date_str[:10] <= self.end.isoformat()


# 校验失败文案；各接口可经 messages 覆盖为自己的历史文案（前端直接展示 detail）
DEFAULT_MESSAGES = {
    'no_custom': '该数据不支持自定义区间',
    'bad_period': 'period 仅支持 {allowed}',
    'bad_date': '{key} 格式应为 YYYY-MM-DD',
    'start_after_end': 'start 不能晚于 end',
    'future_end': 'end 不能晚于今天',
    'too_long': '自定义区间最长 {max_days} 天',
}


def resolve_period(params_get, allowed, default='1m', allow_custom=True,
                   max_span_days=400, today=None, *, max_days=None,
                   allow_future_end=True, custom_default_days=30, messages=None):
    """从查询参数解析区间窗口；非法输入抛 ValueError（视图层转 400）。

    params_get: callable(key)，通常为 request.query_params.get
    max_days: 自定义区间最大跨度（天），缺省沿用旧参数 max_span_days（ARC-04）
    allow_future_end: False 时自定义 end 晚于今天报错（缺省 True，保持旧行为）
    custom_default_days: 自定义区间只给 end 时，start 取 end 往前这么多天
    messages: 覆盖 DEFAULT_MESSAGES 中的文案，保持各接口原有的 400 提示
    """
    today = today or timezone.localdate()
    msg = {**DEFAULT_MESSAGES, **(messages or {})}
    max_days = max_span_days if max_days is None else max_days
    preset = (params_get('period') or '').strip() or default
    start_raw = (params_get('start') or '').strip()
    end_raw = (params_get('end') or '').strip()

    def bad_period():
        return ValueError(msg['bad_period'].format(allowed='/'.join(allowed or [])))

    if preset == 'custom' or start_raw or end_raw:
        if not allow_custom:
            raise ValueError(msg['no_custom'])
        if preset != 'custom' and preset not in (allowed or []):
            raise bad_period()
        return _custom_window(start_raw, end_raw, today, max_days, allow_future_end,
                              custom_default_days, msg)

    if preset not in allowed:
        raise bad_period()
    if preset == 'ytd':
        # 旧实现 _PRESET_DAYS 缺 ytd，FULL_PRESETS 声明支持却抛 KeyError（→502）
        return PeriodWindow(preset, date(today.year, 1, 1), today)
    days = _PRESET_DAYS[preset]
    start = today if days == 1 else today - timedelta(days=days - 1)
    return PeriodWindow(preset, start, today)


def _custom_window(start_raw, end_raw, today, max_days, allow_future_end, default_days, msg):
    def _parse(raw, key, fallback):
        if not raw:
            return fallback
        try:
            return datetime.strptime(raw, '%Y-%m-%d').date()
        except ValueError as e:
            raise ValueError(msg['bad_date'].format(key=key)) from e

    end = _parse(end_raw, 'end', today)
    start = _parse(start_raw, 'start', end - timedelta(days=default_days))
    if start > end:
        raise ValueError(msg['start_after_end'])
    if not allow_future_end and end > today:
        raise ValueError(msg['future_end'])
    if (end - start).days > max_days:
        raise ValueError(msg['too_long'].format(max_days=max_days))
    return PeriodWindow('custom', start, end)


def period_cache_key(prefix, window):
    """带区间的缓存键。"""
    return f'{prefix}_{window.start.isoformat()}_{window.end.isoformat()}'
