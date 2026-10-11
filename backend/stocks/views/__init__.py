"""stocks 视图包（ARC-01：原 stocks/views.py 按领域拆分）。

urls.py 与 `from stocks.views import X` 照旧可用；下列名字为重导出。
注意 patch 目标须写到定义模块，例如 `stocks.views.market.get_market_overview`、
`stocks.views.watchlist.start_fetch_one`——patch 包上的同名引用不影响模块内部调用。
"""

from ..common.upstream import (  # noqa: F401  旧导入路径保留一版
    _BREAKER_OPEN_SECONDS,
    _BREAKER_THRESHOLD,
    _UPSTREAM_POOL,
    UpstreamCircuitOpen,
    UpstreamTimeout,
    _breaker,
    _breaker_lock,
    _call_with_deadline,
    _request_deadline,
    _run_loader,
)
from .alerts import (  # noqa: F401
    _alert_row,
    _alert_rule_error,
    _normalize_threshold,
    alert_detail,
    alert_event_list,
    alert_event_read,
    alert_list,
)
from .groups import _group_row, stock_group_detail, stock_group_list  # noqa: F401
from .market import (  # noqa: F401
    _deadline_error_response,
    _market_bad_request,
    _market_response,
    _optional_float,
    _positive_int,
    _upstream_failed,
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
from .presets import screener_preset_detail, screener_preset_list  # noqa: F401
from .watchlist import StockViewSet, _initial_fetch, _quote_row, dashboard  # noqa: F401
