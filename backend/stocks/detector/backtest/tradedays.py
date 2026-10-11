"""本地交易日历：用基准指数（沪深300）日线日期构造，供回测注入 signals/review/scan。

语义与 stocks.trading_calendar.is_trading_day 一致（三态）：日历覆盖范围内给出
True/False，范围外返回 None（未知）——周期计数/复盘遇到 None 会如实按「日历
不可用」处理，不会把自然日当交易日推进。
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import date


def to_date(value):
    """ISO 字符串 / date → date。"""
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


class ListCalendar:
    """由一组交易日构造的日历（日期升序去重）。"""

    def __init__(self, days):
        uniq = sorted({to_date(d) for d in days})
        if not uniq:
            raise ValueError('交易日历为空')
        self.days = uniq
        self.isos = [d.isoformat() for d in uniq]
        self._index = {d: i for i, d in enumerate(uniq)}
        self._iso_index = {s: i for i, s in enumerate(self.isos)}
        self.first, self.last = uniq[0], uniq[-1]

    def __len__(self):
        return len(self.days)

    def is_trading_day(self, day=None):
        """三态：覆盖范围内 True/False，范围外或未给日期返回 None。"""
        if day is None:
            return None
        day = to_date(day)
        if day < self.first or day > self.last:
            return None
        return day in self._index

    def index_of(self, day):
        """交易日在日历中的序号；非交易日返回 None。"""
        if isinstance(day, str):
            return self._iso_index.get(day[:10])
        return self._index.get(day)

    def next_days(self, after, n):
        """after 之后（不含）的 n 个交易日；不足 n 个返回 None。"""
        i = bisect_right(self.days, to_date(after))
        out = self.days[i:i + n]
        return out if len(out) == n else None

    def prev_days(self, before, n):
        """before 之前（不含）的 n 个交易日，降序；不足返回 None。"""
        i = bisect_left(self.days, to_date(before))
        if i < n:
            return None
        return self.days[i - n:i][::-1]
