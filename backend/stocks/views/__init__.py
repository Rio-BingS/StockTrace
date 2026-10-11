"""stocks 视图包（ARC-01：原 stocks/views.py 按领域拆分）。

这里只重导出 urls.py 用到的公开视图；私有辅助函数与上游熔断工具请从定义模块
导入（views.market / views.watchlist / common.upstream 等），旧名兼容已删除。
patch 目标同样写到定义模块，例如 `stocks.views.market.get_market_overview`。
"""

from .alerts import alert_detail, alert_event_list, alert_event_read, alert_list
from .groups import stock_group_detail, stock_group_list
from .market import (
    market_etf_detail,
    market_etf_radar,
    market_fund_flow_window,
    market_institutions,
    market_national_etf,
    market_national_etf_flow,
    market_northbound_window,
    market_overview,
    market_sectors,
    market_stock_margin,
    market_trend,
)
from .presets import screener_preset_detail, screener_preset_list
from .watchlist import StockViewSet, dashboard

__all__ = [
    'StockViewSet', 'dashboard',
    'market_overview', 'market_trend', 'market_sectors', 'market_national_etf',
    'market_national_etf_flow', 'market_fund_flow_window', 'market_northbound_window',
    'market_etf_radar', 'market_etf_detail', 'market_stock_margin', 'market_institutions',
    'alert_list', 'alert_detail', 'alert_event_list', 'alert_event_read',
    'screener_preset_list', 'screener_preset_detail',
    'stock_group_list', 'stock_group_detail',
]
