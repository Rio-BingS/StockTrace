"""数据源基类与适配层共用的列归一、单位换算工具（从 services 拆出，ARC-01）。

入库单位约定（DAT-03）：成交量统一为「股」，成交额统一为「元」，由各数据源适配器
在返回前换算（东财 手→股 ×100；新浪/BaoStock 原生为股；腾讯日线只有成交量(手)、
无成交额）。
"""

import pandas as pd


class DataSource:
    """数据源基类

    约定：fetch_daily/fetch_minute 返回前须把成交量换算为「股」、成交额换算为「元」
    （拿不到的列置 None），入库层不再做单位假设。
    """
    name = 'base'
    # 分钟 bar 周期（分钟）；BaoStock 只有 5 分钟线
    minute_freq = 1

    def fetch_daily(self, symbol, start_date, end_date):
        """获取日K线，返回 DataFrame[日期,开盘,收盘,最高,最低,成交量,成交额]"""
        raise NotImplementedError

    def fetch_minute(self, symbol, start_dt, end_dt):
        """获取1分钟K线，返回 DataFrame[时间,开盘,收盘,最高,最低,成交量,成交额]"""
        raise NotImplementedError

    def fetch_stock_name(self, code):
        """获取股票名称，返回 str 或 None"""
        return None


def _to_numeric_cols(df, cols=('开盘', '收盘', '最高', '最低', '成交量', '成交额')):
    """把行情数值列转为数值（部分接口返回字符串）"""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return df
    for col in cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    return df


def _volume_lots_to_shares(df):
    """成交量「手」→「股」（×100）"""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return df
    df = _to_numeric_cols(df.copy())
    if '成交量' in df.columns:
        df['成交量'] = df['成交量'] * 100
    return df


def _filter_minute_range(df, start_dt, end_dt):
    """按请求的 [start_dt, end_dt] 过滤分钟 bar（DAT-01）。

    新浪分钟接口无视日期参数，固定返回最近约 8 个交易日；不过滤就会拿多日数据
    求极值并写进错误交易日。'时间' 列统一解析为 Timestamp。
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty or '时间' not in df.columns:
        return df
    t = pd.to_datetime(df['时间'], errors='coerce')
    mask = (t >= pd.Timestamp(start_dt)) & (t <= pd.Timestamp(end_dt))
    out = df.loc[mask].copy()
    out['时间'] = t[mask]
    return out.reset_index(drop=True)


# 列名别名表：(目标列, 英文关键词（小写子串）, 中文关键词（子串）)，按顺序首个命中为准。
# 中英文列名都可能出现（akshare 不同版本），所以按子串匹配而不是精确改名。
_OHLCV_VALUE_ALIASES = (
    ('开盘', ('open',), ('开盘',)),
    ('收盘', ('close',), ('收盘',)),
    ('最高', ('high',), ('最高',)),
    ('最低', ('low',), ('最低',)),
    ('成交量', ('volume',), ('成交量',)),
    ('成交额', ('amount',), ('成交额',)),
)
DAILY_ALIASES = (('日期', ('date',), ('日期',)),) + _OHLCV_VALUE_ALIASES
MINUTE_ALIASES = (('时间', ('day', 'date'), ('时间',)),) + _OHLCV_VALUE_ALIASES


def _normalize_ohlcv(df, aliases):
    """按别名表把行情列统一改名为中文列（日期/时间、开盘…成交额）。

    原新浪日线、分钟线各有一份逐列 if/elif 映射循环，这里合并成一份（A-28）：
    每列按 aliases 顺序匹配，首个命中的目标列生效；没有命中的列原样保留。
    """
    col_map = {}
    for col in df.columns:
        col_lower = str(col).lower()
        for target, en_keys, zh_keys in aliases:
            if any(k in col_lower for k in en_keys) or any(k in col for k in zh_keys):
                col_map[col] = target
                break
    if col_map:
        df = df.rename(columns=col_map)
    return df
