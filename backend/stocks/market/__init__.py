"""
大盘 / 市场数据服务（进程内缓存，适合 1H2G VPS）。

数据源策略：
- 与 stocks.services 类似：多源 failover，而不是死磕东财。
- 源池冷却（cooldown）：某源连续失败后暂时跳过，降低东财限流概率（简易「负载均衡」）。
- 指数优先新浪；北向历史可用；大盘主力东财失败则用北向净买额序列兜底。

模块划分：
- _cache    进程内 TTL 缓存 + 统一 meta
- _sources  数据源冷却与 failover
- _query    通用排序 / 分页
- indices   指数现价与走势
- flows     北向 / 情绪 / 大盘资金历史
- sectors   板块资金轮动
- etf       ETF 现货 / 份额雷达 / 详情 / 国家队观察
- institutions 机构持仓
- overview  聚合总览与收盘后预热

说明：
- 「国家队 ETF」为市场常用宽基/政策相关 ETF 监控列表，非官方实时持仓披露。
- 「机构持仓」来自季报汇总 / 股东变动统计 / 北向资金，非实时成交持仓。
"""

from . import etf, etf_flow, flows, indices, institutions, overview, sectors  # noqa: F401
from ._cache import _cache  # noqa: F401  tests 直接清空共享缓存
from ._cache import _is_fresh  # noqa: F401  tests 使用
from ._sources import get_source_health  # noqa: F401
from .etf import (
    NATIONAL_TEAM_ETFS,
    _etf_row_to_item,  # noqa: F401  旧名保留一版，tests 使用
    get_etf_detail,
    get_etf_share_radar,
    get_national_team_etfs,
)
from .etf_flow import get_national_team_flow
from .flows import (
    fetch_hsgt_flow,
    fetch_market_activity,
    fetch_market_fund_flow_hist,
    get_market_fund_flow_window,
    get_northbound_window,
)
from .margin_stock import fetch_stock_margin
from .indices import MAJOR_INDICES, TREND_INDICES, fetch_index_trend, fetch_major_indices
from .institutions import (
    fetch_institute_hold_stocks,
    fetch_institution_shareholder_changes,
    fetch_northbound_flow_series,
    fetch_stock_institution_detail,
    get_institution_holdings,
)
from .overview import get_market_overview, get_market_trend, warm_post_close_lagging
from .sectors import (
    _parse_fund_flow_table,  # noqa: F401  旧名保留一版，tests 使用
    _parse_sector_rank_table,  # noqa: F401  tests 使用
    _sector_payload,  # noqa: F401  tests 使用
    fetch_concept_fund_flow,
    fetch_industry_fund_flow,
    get_sector_rotation,
)

# 口径解析的公开名（validate_market_sources 使用，ARC-01）
etf_row_to_item = _etf_row_to_item
parse_fund_flow_table = _parse_fund_flow_table

__all__ = [
    # 视图 / 调度主入口
    'get_market_overview',
    'fetch_stock_margin',
    'get_market_trend',
    'get_sector_rotation',
    'get_national_team_etfs',
    'get_national_team_flow',
    'get_market_fund_flow_window',
    'get_northbound_window',
    'get_etf_share_radar',
    'get_etf_detail',
    'get_institution_holdings',
    'warm_post_close_lagging',
    # 各域抓取函数
    'fetch_major_indices',
    'fetch_index_trend',
    'fetch_hsgt_flow',
    'fetch_market_activity',
    'fetch_market_fund_flow_hist',
    'fetch_concept_fund_flow',
    'fetch_industry_fund_flow',
    'fetch_institute_hold_stocks',
    'fetch_institution_shareholder_changes',
    'fetch_northbound_flow_series',
    'fetch_stock_institution_detail',
    'get_source_health',
    # 口径解析（校验命令用）
    'etf_row_to_item',
    'parse_fund_flow_table',
    # 常量
    'MAJOR_INDICES',
    'TREND_INDICES',
    'NATIONAL_TEAM_ETFS',
]
