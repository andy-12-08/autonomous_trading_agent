from datetime import datetime

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient

import config

from core.broker_orders import OrdersMixin
from core.broker_data import MarketDataMixin


class AlpacaBroker(OrdersMixin, MarketDataMixin):
    """Alpaca paper trading client plus historical data and market-data helpers."""

    _ALPACA_TIMEOUT = 30
    NEWS_CACHE_TTL_MIN = 15

    def __init__(self):
        """Create trading and data REST clients and enforce HTTP timeouts on their sessions."""
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
        self._news_stream: object | None = None

    def get_account(self):
        """Fetch the live Alpaca account snapshot.

        Returns:
            Alpaca account object with equity, cash, and status fields.
        """
        return self._trade_client.get_account()

    def get_positions(self) -> dict:
        """Return all open positions keyed by symbol.

        Returns:
            Dict mapping uppercase symbol strings to Alpaca position objects.
        """
        positions = self._trade_client.get_all_positions()
        return {p.symbol: p for p in positions}

    def get_open_orders(self) -> list:
        """Return every order currently open at the broker.

        Returns:
            List of Alpaca order objects with open status.
        """
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        return self._trade_client.get_orders(filter=req)

    def has_active_stop_order(self, symbol: str, open_orders: list) -> bool:
        """Return True if a stop-type sell order (not a take-profit limit) exists for symbol.

        Bracket child legs often have no symbol attribute of their own — the symbol
        lives only on the parent order.  We therefore accept a leg as matching when
        the parent's symbol matches, even if the leg's own symbol field is empty.

        Args:
            symbol: Equity ticker to inspect.
            open_orders: Iterable returned by get_open_orders.

        Returns:
            True when a stop or stop_limit sell is open; False when only a TP limit exists.
        """
        _STOP_TYPES = {"stop", "stop_limit", "trailing_stop"}
        sym_up = symbol.upper()

        def _is_stop_sell(o, inherited_sym: str = "") -> bool:
            o_sym  = str(getattr(o, "symbol", "") or inherited_sym).upper()
            side   = str(getattr(o, "side",   "")).lower()
            o_type = str(getattr(o, "order_type", "") or getattr(o, "type", "") or "").lower()
            return o_sym == sym_up and "sell" in side and o_type in _STOP_TYPES

        for o in open_orders:
            if _is_stop_sell(o):
                return True
            parent_sym = str(getattr(o, "symbol", "")).upper()
            for leg in (getattr(o, "legs", None) or []):
                if _is_stop_sell(leg, inherited_sym=parent_sym):
                    return True
        return False

    def is_market_open(self) -> bool:
        """Ask Alpaca for the session clock, or fall back to a simple ET weekday heuristic.

        Returns:
            True when the regular session is considered open, otherwise False.
        """
        try:
            clock = self._trade_client.get_clock()
            return clock.is_open
        except Exception:
            import datetime as _dt

            now_et = _dt.datetime.now(config.ET)
            return (now_et.weekday() < 5 and
                    _dt.time(9, 30) <= now_et.time() <= _dt.time(16, 0))
