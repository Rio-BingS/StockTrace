"""BaoStock 数据源（query_history_k_data_plus，日线 + 5 分钟线）。"""

import pandas as pd

from ..baostock_session import bs_session as _bs_session
from ..common.codes import exchange, exchange_prefix
from .base import DataSource, _to_numeric_cols


class BaoStockSource(DataSource):
    """BaoStock 数据源 - 独立服务器，不受东方财富网络限制

    成交量为「股」、成交额为「元」，无需换算；分钟线只有 5 分钟周期。
    """
    name = 'baostock'
    minute_freq = 5

    @staticmethod
    def _format_code(code):
        """6位代码转 BaoStock 格式: sz.000001, sh.600000；北交所不支持返回 None"""
        if exchange(code) == 'bj':
            return None
        return exchange_prefix(code, sep='.')

    def fetch_daily(self, symbol, start_date, end_date):
        bs_code = self._format_code(symbol)
        if bs_code is None:
            # BaoStock 不支持北交所，跳过该源（DAT-12）
            return None
        # start_date/end_date 格式: YYYYMMDD → YYYY-MM-DD
        sd = f'{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}'
        ed = f'{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}'

        with _bs_session() as bs:
            rs = bs.query_history_k_data_plus(
                bs_code,
                'date,open,high,low,close,volume,amount',
                start_date=sd, end_date=ed,
                frequency='d', adjustflag='2'
            )
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=rs.fields)
        df = df.rename(columns={
            'date': '日期', 'open': '开盘', 'high': '最高',
            'low': '最低', 'close': '收盘', 'volume': '成交量',
            'amount': '成交额',
        })
        # 转换数值类型
        return _to_numeric_cols(df)

    def fetch_minute(self, symbol, start_dt, end_dt):
        bs_code = self._format_code(symbol)
        if bs_code is None:
            return None
        # start_dt 格式: 'YYYY-MM-DD HH:MM:SS'，提取日期
        date_str = start_dt[:10] if ' ' in start_dt else start_dt

        with _bs_session() as bs:
            # BaoStock 支持5分钟(frequency='5')，不支持1分钟
            rs = bs.query_history_k_data_plus(
                bs_code,
                'date,time,open,high,low,close,volume,amount',
                start_date=date_str, end_date=end_dt[:10] if ' ' in end_dt else end_dt,
                frequency='5', adjustflag='2'
            )
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=rs.fields)
        # BaoStock 时间格式: '20260612093500000'，解析为 datetime
        df['时间'] = pd.to_datetime(df['time'], format='%Y%m%d%H%M%S%f', errors='coerce')
        df = df.rename(columns={
            'open': '开盘', 'high': '最高', 'low': '最低',
            'close': '收盘', 'volume': '成交量', 'amount': '成交额',
        })
        return _to_numeric_cols(df)

    def fetch_stock_name(self, code):
        bs_code = self._format_code(code)
        if bs_code is None:
            return None
        with _bs_session() as bs:
            rs = bs.query_stock_basic(code=bs_code)
            while rs.next():
                row = rs.get_row_data()
                return row[1]  # code_name 字段
        return None
