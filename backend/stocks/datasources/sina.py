"""新浪数据源（AkShare stock_zh_a_daily / stock_zh_a_minute）。"""

import akshare as ak

from ..common.codes import exchange_prefix
from .base import DAILY_ALIASES, MINUTE_ALIASES, DataSource, _normalize_ohlcv, _to_numeric_cols


class SinaSource(DataSource):
    """新浪数据源"""
    name = 'sina'

    def fetch_daily(self, symbol, start_date, end_date):
        # 新浪接口需要 sh/sz 前缀
        # 注意：akshare stock_zh_a_daily 内部已把 OHLC round 到 2 位，ETF 走此源时
        # 第 3 位小数已丢失；库字段虽为 3 位（DAT-11），此源无法恢复精度
        bs_code = exchange_prefix(symbol)
        df = ak.stock_zh_a_daily(
            symbol=bs_code, start_date=start_date,
            end_date=end_date, adjust="qfq"
        )
        return _normalize_ohlcv(df, DAILY_ALIASES)

    def fetch_minute(self, symbol, start_dt, end_dt):
        # 新浪分钟线需要 sh 前缀
        bs_code = exchange_prefix(symbol)
        # 注意：该接口固定返回最近 1970 根 bar（约 8 个交易日），不接受日期参数；
        # 按请求区间过滤在 _try_sources 统一做（DAT-01）
        df = ak.stock_zh_a_minute(
            symbol=bs_code, period="1"
        )
        df = _normalize_ohlcv(df, MINUTE_ALIASES)
        # 新浪分钟线成交量为「股」、成交额为「元」，原样保留；值为字符串，转数值
        return _to_numeric_cols(df)

    def fetch_stock_name(self, code):
        try:
            df = ak.stock_zh_a_spot()
            # 新浪返回列名编码可能不同，用位置匹配
            code_col = df.columns[0]  # 代码列
            name_col = df.columns[1]  # 名称列
            bs_code = exchange_prefix(code)
            row = df[df[code_col] == bs_code]
            if not row.empty:
                return row.iloc[0][name_col]
        except Exception:
            pass
        return None
