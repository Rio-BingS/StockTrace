"""东方财富数据源（AkShare stock_zh_a_hist / stock_zh_a_hist_min_em）。"""

import akshare as ak

from .base import DataSource, _volume_lots_to_shares


class EastMoneySource(DataSource):
    """东方财富数据源"""
    name = 'eastmoney'

    def fetch_daily(self, symbol, start_date, end_date):
        df = ak.stock_zh_a_hist(
            symbol=symbol, period="daily",
            start_date=start_date, end_date=end_date, adjust="qfq"
        )
        # 东财成交量单位为「手」，统一换算为「股」；成交额为元
        return _volume_lots_to_shares(df)

    def fetch_minute(self, symbol, start_dt, end_dt):
        df = ak.stock_zh_a_hist_min_em(
            symbol=symbol, period="1",
            start_date=start_dt, end_date=end_dt, adjust="qfq"
        )
        # 东财分钟线成交量同为「手」
        return _volume_lots_to_shares(df)

    def fetch_stock_name(self, code):
        df = ak.stock_zh_a_spot_em()
        row = df[df['代码'] == code]
        if not row.empty:
            return row.iloc[0]['名称']
        return None
