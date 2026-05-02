import time
import requests as _requests
from datetime import datetime, timedelta, date, timezone

import pandas as pd

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    StopOrderRequest,
    GetOrdersRequest,
    GetAssetsRequest,
)
from alpaca.trading.enums import (
    OrderSide, TimeInForce,
    QueryOrderStatus, AssetClass, AssetStatus,
)
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestQuoteRequest,
    StockSnapshotRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

import config
from core.database import log

from core.broker_orders import OrdersMixin
from core.broker_data import MarketDataMixin


class AlpacaBroker(OrdersMixin, MarketDataMixin):
    _ALPACA_TIMEOUT = 30
    NEWS_CACHE_TTL_MIN = 15

    def __init__(self):
        self._trade_client = TradingClient(config.ALPACA_KEY, config.ALPACA_SECRET, paper=True)
        self._data_client = StockHistoricalDataClient(config.ALPACA_KEY, config.ALPACA_SECRET)

        for _c in (self._trade_client, self._data_client):
            if hasattr(_c, "_session"):
                _orig = _c._session.request
                def _make_patched(orig):
                    def _patched(method, url, **kw):
                        kw["timeout"] = self._ALPACA_TIMEOUT
                        return orig(method, url, **kw)
                    return _patched
                _c._session.request = _make_patched(_orig)

        self._asset_cache: list[str] = []
        self._asset_cache_date: str = ""
        self._news_cache: dict[str, list] = {}
        self._news_cache_ts: datetime | None = None

    def get_account(self):
        """Returns:
            Alpaca account object (equity, cash, status).
        """
        return self._trade_client.get_account()

    def get_positions(self) -> dict:
        """Returns:
            Mapping symbol -> Alpaca position object.
        """
        positions = self._trade_client.get_all_positions()
        return {p.symbol: p for p in positions}

    def get_open_orders(self) -> list:
        """Returns:
            List of open Alpaca order objects.
        """
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        return self._trade_client.get_orders(filter=req)

    def has_active_stop_order(self, symbol: str, open_orders: list) -> bool:
        """True if any open sell order exists for symbol (including bracket legs).

        Args:
            symbol: Ticker.
            open_orders: Result of get_open_orders.

        Returns:
            Boolean.
        """
        def _is_sell(o) -> bool:
            sym  = str(getattr(o, "symbol", "")).upper()
            side = str(getattr(o, "side",   "")).lower()
            return sym == symbol.upper() and "sell" in side

        for o in open_orders:
            if _is_sell(o):
                return True
            for leg in (getattr(o, "legs", None) or []):
                if _is_sell(leg):
                    return True
        return False

    def is_market_open(self) -> bool:
        """Returns:
            True if the equities session is open (API clock, else ET heuristic).
        """
        try:
            clock = self._trade_client.get_clock()
            return clock.is_open
        except Exception:
            import datetime as _dt

            now_et = _dt.datetime.now(config.ET)
            return (now_et.weekday() < 5 and
                    _dt.time(9, 30) <= now_et.time() <= _dt.time(16, 0))
