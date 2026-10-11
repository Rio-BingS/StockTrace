"""腾讯数据源（AkShare stock_zh_a_hist_tx，仅日线）。"""

import akshare as ak

from ..common.codes import exchange_prefix
from .base import DataSource, _volume_lots_to_shares


class TencentSource(DataSource):
    """腾讯数据源"""
    name = 'tencent'

    def fetch_daily(self, symbol, start_date, end_date):
        # 腾讯接口需要 sh/sz 前缀
        bs_code = exchange_prefix(symbol)
        df = ak.stock_zh_a_hist_tx(
            symbol=bs_code, period="daily",
            start_date=start_date, end_date=end_date, adjust="qfq"
        )
        if df is None or df.empty:
            return df
        # akshare 1.18.64 的列为 date,open,close,high,low,amount，其中 amount 实际是
        # 成交量（手），并不是成交额：映射为成交量并换算为股，成交额如实置 None。
        # 这里 amount 的含义与通用别名表相反，所以保留精确改名、不走 _normalize_ohlcv
        df = df.rename(columns={
            'date': '日期', 'open': '开盘', 'close': '收盘',
            'high': '最高', 'low': '最低', 'amount': '成交量',
        })
        df = _volume_lots_to_shares(df)
        df['成交额'] = None
        return df

    def fetch_minute(self, symbol, start_dt, end_dt):
        # 腾讯分钟线接口(stock_zh_a_tick_tx_js)是逐笔数据，非K线，格式不匹配
        return None

    def fetch_stock_name(self, code):
        return None
