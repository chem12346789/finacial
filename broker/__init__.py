"""Gateway-free Futunn REST API client (not the legacy futu-api SDK)."""

from .client import (
    AuthorizationSession,
    FutuAPIError,
    FutuError,
    FutuTransportError,
    OAuthClient,
    OrderConfirmationRequired,
    TokenSet,
    TradingDisabledError,
)
from .futu_client import FutuClient
from .sim_trading_client import SimTradingClient
from .market_data import (
    CachedHistoryClient,
    HistoryClient,
    ThrottledHistoryClient,
    fetch_daily_klines,
    futu_bar_date,
    get_default_quote_client,
)

__all__ = [
    "AuthorizationSession",
    "CachedHistoryClient",
    "FutuAPIError",
    "FutuClient",
    "FutuError",
    "FutuTransportError",
    "HistoryClient",
    "OAuthClient",
    "OrderConfirmationRequired",
    "TokenSet",
    "ThrottledHistoryClient",
    "TradingDisabledError",
    "SimTradingClient",
    "fetch_daily_klines",
    "futu_bar_date",
    "get_default_quote_client",
]
