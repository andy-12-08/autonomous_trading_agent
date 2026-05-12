import concurrent.futures as _cf
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests as _requests
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestQuoteRequest,
    StockSnapshotRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.enums import AssetClass, AssetStatus, QueryOrderStatus
from alpaca.trading.requests import GetAssetsRequest, GetOrdersRequest

import config
from core.database import log


class MarketDataMixin:
    """Bars, quotes, snapshots, asset lists, fills, and news for AlpacaBroker."""

    def get_bars(self, symbol: str, timeframe: str = "5Min", days: int = 5) -> pd.DataFrame:
        """Fetch OHLCV bar data for a single symbol.

        Args:
            symbol: Ticker symbol to fetch bars for.
            timeframe: Bar width — one of "1Min", "5Min", "15Min", "1Hour", "1Day".
            days: Number of calendar days of history to request.

        Returns:
            DataFrame indexed by timestamp with OHLCV columns, or an empty
            DataFrame if the request fails.
        """
        tf_map = {
            "1Min":  TimeFrame.Minute,
            "5Min":  TimeFrame(5,  TimeFrameUnit.Minute),
            "15Min": TimeFrame(15, TimeFrameUnit.Minute),
            "1Hour": TimeFrame.Hour,
            "1Day":  TimeFrame.Day,
        }
        tf = tf_map.get(timeframe, TimeFrame(5, TimeFrameUnit.Minute))
        end = datetime.now(config.ET)
        start = end - timedelta(days=days)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            start=start,
            end=end,
            feed="iex",
        )
        try:
            bars = self._data_client.get_stock_bars(req)
            df = bars.df
            if isinstance(df.index, pd.MultiIndex):
                df = df.xs(symbol, level=0)
            df = df.sort_index()
            return df
        except Exception as e:
            log.warning("get_bars failed for %s: %s", symbol, e)
            return pd.DataFrame()

    def get_bars_multi(self, symbols: list[str], timeframe: str = "5Min",
                       days: int = 5) -> dict[str, pd.DataFrame]:
        """Fetch OHLCV bars for many symbols in one API call (IEX feed).

        Args:
            symbols: Ticker list; empty input returns an empty dict.
            timeframe: Bar width such as 1Min, 5Min, 15Min, 1Hour, or 1Day.
            days: Calendar days of history to request backward from now.

        Returns:
            Dict mapping each symbol with data to its sorted OHLCV DataFrame;
            symbols without IEX data are omitted.
        """
        if not symbols:
            return {}
        tf_map = {
            "1Min":  TimeFrame.Minute,
            "5Min":  TimeFrame(5,  TimeFrameUnit.Minute),
            "15Min": TimeFrame(15, TimeFrameUnit.Minute),
            "1Hour": TimeFrame.Hour,
            "1Day":  TimeFrame.Day,
        }
        tf    = tf_map.get(timeframe, TimeFrame(5, TimeFrameUnit.Minute))
        end   = datetime.now(config.ET)
        start = end - timedelta(days=days)
        req   = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=tf,
            start=start,
            end=end,
            feed="iex",
        )
        _TIMEOUT = 45
        _pool = _cf.ThreadPoolExecutor(max_workers=1)
        _fut  = _pool.submit(self._data_client.get_stock_bars, req)
        try:
            bars = _fut.result(timeout=_TIMEOUT)
            _pool.shutdown(wait=False)
        except _cf.TimeoutError:
            _pool.shutdown(wait=False)
            log.warning("get_bars_multi timed out after %ds (%d symbols, %s) — returning empty",
                        _TIMEOUT, len(symbols), timeframe)
            return {}
        except Exception as e:
            _pool.shutdown(wait=False)
            log.warning("get_bars_multi failed (%d symbols, %s): %s", len(symbols), timeframe, e)
            return {}

        df_all = bars.df
        result: dict[str, pd.DataFrame] = {}
        if isinstance(df_all.index, pd.MultiIndex):
            for sym in symbols:
                try:
                    df_sym = df_all.xs(sym, level=0).sort_index()
                    if not df_sym.empty:
                        result[sym] = df_sym
                except KeyError:
                    pass
        elif not df_all.empty and len(symbols) == 1:
            result[symbols[0]] = df_all.sort_index()
        return result

    def get_latest_price(self, symbol: str) -> float | None:
        """Return the most recent close price for a symbol.

        Args:
            symbol: Ticker symbol to look up.

        Returns:
            Latest close price as a float, or None if data is unavailable.
        """
        df = self.get_bars(symbol, "1Min", days=1)
        if df.empty:
            return None
        return float(df["close"].iloc[-1])

    def get_latest_quote(self, symbol: str) -> dict | None:
        """Return bid/ask quote data for Rule 1 tight-spread checks.

        Discards stale, incomplete, or implausibly wide quotes (>5% spread)
        rather than blocking a valid setup.

        Args:
            symbol: Ticker symbol to fetch a quote for.

        Returns:
            Dict with bid, ask, spread, and spread_pct keys, or None when the
            quote is missing, invalid, or fails sanity checks.
        """
        try:
            req  = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            resp = self._data_client.get_stock_latest_quote(req)
            quote = resp[symbol]
            bid = float(quote.bid_price or 0)
            ask = float(quote.ask_price or 0)
            if bid <= 0 or ask <= 0 or ask < bid:
                return None
            mid = (bid + ask) / 2
            spread_pct = (ask - bid) / mid
            if spread_pct > 0.05:
                log.warning("Discarding implausible quote for %s: bid=%.2f ask=%.2f spread=%.2f%%",
                            symbol, bid, ask, spread_pct * 100)
                return None
            return {"bid": bid, "ask": ask, "spread": round(ask - bid, 4),
                    "spread_pct": round(spread_pct, 6)}
        except Exception as e:
            log.warning("get_latest_quote failed for %s: %s", symbol, e)
            return None

    def get_news_headlines(self, symbols: list[str], hours_back: int = 18) -> dict[str, list[dict]]:
        """Fetch recent headlines per symbol via Alpaca news (Benzinga), with REST caching.

        When broker._news_stream is set, WebSocket articles are merged in so fresh
        headlines appear without waiting for the next REST poll.

        Args:
            symbols: Tickers to request; absent keys mean no articles returned.
            hours_back: Hours of history to include from the REST endpoint.

        Returns:
            Dict mapping symbol to a list of dicts with headline, summary, and created_at.
        """
        if not symbols:
            return {}

        now = datetime.now(config.ET)
        if (self._news_cache_ts is not None and
                (now - self._news_cache_ts).total_seconds() < self.NEWS_CACHE_TTL_MIN * 60):
            return {s: self._news_cache[s] for s in symbols if s in self._news_cache}

        start = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        headers = {
            "APCA-API-KEY-ID":     config.ALPACA_KEY or "",
            "APCA-API-SECRET-KEY": config.ALPACA_SECRET or "",
            "accept": "application/json",
        }
        result: dict[str, list[dict]] = {}
        BATCH = 50
        for i in range(0, len(symbols), BATCH):
            batch = symbols[i : i + BATCH]
            try:
                resp = _requests.get(
                    "https://data.alpaca.markets/v1beta1/news",
                    headers=headers,
                    params={
                        "symbols":         ",".join(batch),
                        "start":           start,
                        "limit":           50,
                        "sort":            "desc",
                        "include_content": "false",
                    },
                    timeout=8,
                )
                resp.raise_for_status()
                for article in resp.json().get("news", []):
                    headline   = article.get("headline", "")
                    summary    = (article.get("summary") or "")[:200]
                    created_at = article.get("created_at", "")
                    for sym in article.get("symbols", []):
                        if sym in batch:
                            result.setdefault(sym, []).append({
                                "headline":   headline,
                                "summary":    summary,
                                "created_at": created_at,
                            })
            except Exception as e:
                log.warning("get_news_headlines failed (batch %d): %s", i, e)

        self._news_cache    = result
        self._news_cache_ts = now

        stream = getattr(self, "_news_stream", None)
        if stream is not None:
            stream_news = stream.get_news(symbols, max_age_minutes=30)
            for sym, articles in stream_news.items():
                existing_headlines = {a["headline"] for a in result.get(sym, [])}
                fresh = [a for a in articles if a["headline"] not in existing_headlines]
                if fresh:
                    result[sym] = fresh + result.get(sym, [])

        stream_label = " (stream active)" if (stream and getattr(stream, "is_connected", False)) else ""
        log.info("News: %d/%d watchlist symbols have headlines%s",
                 len(result), len(symbols), stream_label)
        return {s: result[s] for s in symbols if s in result}

    def get_all_tradeable_symbols(self) -> list[str]:
        """Return all active, tradeable US equity symbols on NYSE and NASDAQ.

        Result is cached for the trading session — the asset list doesn't change intraday.

        Returns:
            List of ticker symbol strings (alpha-only, 1–5 characters) that are
            active and tradeable on NYSE, NASDAQ, ARCA, or BATS.
        """
        today = date.today().isoformat()
        if self._asset_cache and self._asset_cache_date == today:
            return self._asset_cache

        try:
            req    = GetAssetsRequest(asset_class=AssetClass.US_EQUITY,
                                      status=AssetStatus.ACTIVE)
            assets = self._trade_client.get_all_assets(req)
            symbols: list[str] = []
            for a in assets:
                sym      = str(getattr(a, "symbol",   "") or "").strip().upper()
                tradable = bool(getattr(a, "tradable", False))
                exchange_raw = getattr(a, "exchange", None)
                exchange = (exchange_raw.value
                            if hasattr(exchange_raw, "value") else str(exchange_raw or ""))
                if (tradable
                        and exchange in ("NYSE", "NASDAQ", "ARCA", "BATS")
                        and sym.isalpha()
                        and 1 <= len(sym) <= 5):
                    symbols.append(sym)
            self._asset_cache      = symbols
            self._asset_cache_date = today
            log.info("Asset list cached: %d tradeable NYSE/NASDAQ symbols", len(symbols))
            return symbols
        except Exception as e:
            log.warning("get_all_tradeable_symbols failed: %s", e)
            return []

    def get_snapshots_bulk(self, symbols: list[str]) -> dict[str, dict]:
        """Fetch price, volume, dollar volume, and percent change for many symbols (IEX).

        Args:
            symbols: Tickers to snapshot; large lists are processed in batches of 500.

        Returns:
            Dict mapping symbol to price, volume, dollar_volume, and change_pct;
            symbols without IEX snapshot data are omitted.
        """
        if not symbols:
            return {}

        BATCH  = 500
        result: dict[str, dict] = {}

        _SNAP_TIMEOUT = 30
        for i in range(0, len(symbols), BATCH):
            batch = symbols[i : i + BATCH]
            req   = StockSnapshotRequest(symbol_or_symbols=batch, feed="iex")
            _pool = _cf.ThreadPoolExecutor(max_workers=1)
            _fut  = _pool.submit(self._data_client.get_stock_snapshot, req)
            try:
                snaps = _fut.result(timeout=_SNAP_TIMEOUT)
                _pool.shutdown(wait=False)
            except _cf.TimeoutError:
                _pool.shutdown(wait=False)
                log.warning("Snapshot batch timed out after %ds (offset=%d n=%d) — skipping",
                            _SNAP_TIMEOUT, i, len(batch))
                time.sleep(0.2)
                continue
            except Exception as e:
                _pool.shutdown(wait=False)
                log.warning("Snapshot batch failed (offset=%d n=%d): %s", i, len(batch), e)
                time.sleep(0.2)
                continue

            for sym, snap in snaps.items():
                try:
                    daily      = getattr(snap, "daily_bar",      None)
                    prev       = getattr(snap, "prev_daily_bar", None)
                    latest     = getattr(snap, "latest_trade",   None)
                    price      = float(getattr(latest, "price",  0) or 0)
                    volume     = float(getattr(daily,  "volume", 0) or 0)
                    prev_close = float(getattr(prev,   "close",  0) or 0)
                    if price <= 0 or volume <= 0:
                        continue
                    if prev_close > 0:
                        change_pct = (price - prev_close) / prev_close * 100
                    else:
                        # IEX snapshot does not populate prev_daily_bar — fall back to
                        # today's open so we can still identify intraday movers.
                        day_open = float(getattr(daily, "open", 0) or 0)
                        change_pct = ((price - day_open) / day_open * 100
                                      if day_open > 0 else 0.0)
                    result[sym] = {
                        "price":        round(price,         2),
                        "volume":       int(volume),
                        "dollar_volume": round(price * volume, 0),
                        "change_pct":   round(change_pct,    2),
                    }
                except Exception:
                    pass
            time.sleep(0.1)  # pace requests to avoid IEX rate-limit accumulation

        return result

    def get_last_filled_sell(self, symbol: str) -> dict | None:
        """Return fill data for the most recently filled SELL for symbol today.

        Bracket stop/TP fills happen on child legs, not the parent order.
        This function checks three sources in order:
          1. Closed orders — parent AND nested legs
          2. Account activities (FILL type) — most reliable for bracket legs

        Args:
            symbol: Ticker symbol to check for filled sell orders.

        Returns:
            Dict with keys {fill_price, qty, filled_at}, or None if not found.
        """
        sym_up      = symbol.upper()
        today_start = datetime.combine(date.today(), datetime.min.time()).replace(
            tzinfo=timezone.utc)

        def _extract_fill(o) -> dict | None:
            if str(getattr(o, "symbol", "")).upper() != sym_up:
                return None
            side   = str(getattr(o, "side",            "")).lower()
            status = str(getattr(o, "status",          "")).lower()
            fill   = getattr(o, "filled_avg_price", None)
            qty    = getattr(o, "filled_qty",       None)
            if "sell" in side and status == "filled" and fill:
                return {
                    "fill_price": float(fill),
                    "qty":        float(qty or 0),
                    "filled_at":  str(getattr(o, "filled_at", "") or ""),
                }
            return None

        # Pass 1: closed orders — check parent orders and all bracket legs
        try:
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                symbols=[symbol],
                limit=20,
                after=today_start,
            )
            orders = self._trade_client.get_orders(filter=req)
            for o in orders:
                hit = _extract_fill(o)
                if hit:
                    return hit
                for leg in (getattr(o, "legs", None) or []):
                    hit = _extract_fill(leg)
                    if hit:
                        return hit
        except Exception as e:
            log.warning("get_last_filled_sell pass1 failed for %s: %s", symbol, e)

        # Pass 2: account activities — catches bracket leg fills that are not
        # surfaced at the top level of the orders list
        try:
            from alpaca.trading.requests import GetAccountActivitiesRequest
            from alpaca.trading.enums import ActivityType
            acts = self._trade_client.get_account_activities(
                GetAccountActivitiesRequest(
                    activity_types=[ActivityType.FILL],
                    date=date.today().isoformat(),
                )
            )
            for act in (acts or []):
                act_sym   = str(getattr(act, "symbol", "")).upper()
                act_side  = str(getattr(act, "side",   "")).lower()
                act_price = getattr(act, "price",      None)
                act_qty   = getattr(act, "qty",        None)
                act_ts    = str(getattr(act, "transaction_time", "") or "")
                if act_sym == sym_up and "sell" in act_side and act_price:
                    log.info("get_last_filled_sell %s: found via activities fill=%.4f",
                             symbol, float(act_price))
                    return {
                        "fill_price": float(act_price),
                        "qty":        float(act_qty or 0),
                        "filled_at":  act_ts,
                    }
        except Exception as e:
            log.warning("get_last_filled_sell pass2 (activities) failed for %s: %s", symbol, e)

        return None

    def get_fill_price(self, order_id: str, retries: int = 3, delay: float = 0.5) -> float | None:
        """Poll for the actual fill price of a just-submitted market order.

        Paper market orders fill almost instantly; retries cover the brief lag.

        Args:
            order_id: Alpaca order ID string to poll.
            retries: Number of polling attempts before giving up (default 3).
            delay: Seconds to wait between each retry (default 0.5).

        Returns:
            filled_avg_price as a float, or None if not filled within the retry window.
        """
        for _ in range(retries):
            try:
                order = self._trade_client.get_order_by_id(str(order_id))
                fill  = getattr(order, "filled_avg_price", None)
                if fill:
                    return float(fill)
            except Exception:
                pass
            time.sleep(delay)
        return None

