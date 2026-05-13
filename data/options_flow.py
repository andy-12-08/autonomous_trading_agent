"""
Options flow scanner: detects institutional positioning signals from live
options chains, including unusual volume, put/call ratios, and sweep activity.

Also provides the data layer for the IV analyzer — fetching ATM IV, available
expirations, and full chains for strike selection.

Data source: yfinance (free CBOE/OCC options chains).
Cache TTL: 30 minutes (chains update continuously but one snapshot per scan is sufficient).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

import yfinance as yf

import config
from core.database import log


class OptionsFlowClient:
    """
    Options chain scanner providing flow signals and raw chain data.

    Detects:
      - Unusual call/put volume (volume > 2× open interest → institutional sweep)
      - Put/call ratio imbalance (bullish if P/C < 0.7, bearish if P/C > 1.5)
      - High-conviction flow: both unusual AND directional (strongest signal)

    All results are cached for 30 minutes to limit API calls during scan cycles.
    """

    _CACHE_TTL_SECONDS = 1800   # 30 minutes

    def __init__(self) -> None:
        """Initialize with empty caches."""
        self._flow_cache:    dict[str, dict]     = {}
        self._flow_cache_ts: Optional[datetime]  = None
        self._chain_cache:   dict[str, object]   = {}   # raw yfinance chains
        self._chain_ts:      dict[str, datetime] = {}

    # ── Public: flow signals ──────────────────────────────────────────────────

    def get_options_flow(
        self, symbols: list[str], max_symbols: int = 30
    ) -> dict[str, dict]:
        """
        Fetch nearest-expiry options flow for each symbol and classify activity.

        Args:
            symbols:     List of tickers to scan.
            max_symbols: Cap on symbols fetched per call (API rate protection).

        Returns:
            Dict mapping symbol to a flow classification dict containing:
              unusual_calls   – True if call volume > 2× open interest
              unusual_puts    – True if put volume > 2× open interest
              put_call_ratio  – put volume / call volume
              call_volume     – total calls traded today
              put_volume      – total puts traded today
              call_oi         – total call open interest
              put_oi          – total put open interest
              signal          – 'unusual_calls' | 'unusual_puts' | 'bullish_flow'
                                | 'bearish_flow' | 'neutral'
              high_conviction – True if unusual AND directional
              expiry          – nearest expiry date used
            Symbols with thin markets (<100 total contracts) are omitted.
        """
        now = datetime.now()
        if (self._flow_cache_ts is not None
                and (now - self._flow_cache_ts).total_seconds() < self._CACHE_TTL_SECONDS):
            return {s: self._flow_cache[s] for s in symbols if s in self._flow_cache}

        result: dict[str, dict] = {}
        today  = date.today()

        for sym in symbols[:max_symbols]:
            try:
                ticker      = yf.Ticker(sym)
                expirations = ticker.options
                if not expirations:
                    continue

                qualifying = [
                    e for e in expirations
                    if (date.fromisoformat(e) - today).days >= 7
                ]
                exp   = qualifying[0] if qualifying else expirations[0]
                chain = ticker.option_chain(exp)
                calls = chain.calls
                puts  = chain.puts
                if calls.empty or puts.empty:
                    continue

                call_vol = float(calls["volume"].fillna(0).sum())
                put_vol  = float(puts["volume"].fillna(0).sum())
                call_oi  = float(calls["openInterest"].fillna(0).sum())
                put_oi   = float(puts["openInterest"].fillna(0).sum())

                if call_vol + put_vol < 100:
                    continue

                pc_ratio      = put_vol / call_vol if call_vol > 0 else 9.99
                unusual_calls = bool(call_oi > 0 and call_vol > call_oi * 2.0)
                unusual_puts  = bool(put_oi  > 0 and put_vol  > put_oi  * 2.0)

                if unusual_calls:
                    signal = "unusual_calls"
                elif unusual_puts:
                    signal = "unusual_puts"
                elif pc_ratio < 0.7:
                    signal = "bullish_flow"
                elif pc_ratio > 1.5:
                    signal = "bearish_flow"
                else:
                    signal = "neutral"

                high_conviction = (
                    (unusual_calls and pc_ratio < 0.8) or
                    (unusual_puts  and pc_ratio > 1.3)
                )

                result[sym] = {
                    "unusual_calls":    unusual_calls,
                    "unusual_puts":     unusual_puts,
                    "put_call_ratio":   round(pc_ratio, 2),
                    "call_volume":      int(call_vol),
                    "put_volume":       int(put_vol),
                    "call_oi":          int(call_oi),
                    "put_oi":           int(put_oi),
                    "signal":           signal,
                    "high_conviction":  high_conviction,
                    "expiry":           exp,
                }

            except Exception as exc:
                log.debug("Options flow failed %s: %s", sym, exc)

        self._flow_cache    = result
        self._flow_cache_ts = now
        unusual_syms = [s for s, d in result.items() if d.get("unusual_calls") or d.get("unusual_puts")]
        log.info("Options flow: %d/%d symbols | unusual activity: %s",
                 len(result), len(symbols[:max_symbols]), unusual_syms or "none")
        return {s: result[s] for s in symbols if s in result}

    # ── Public: raw chain data for execution ─────────────────────────────────

    def get_chain(self, symbol: str, expiry: str) -> Optional[object]:
        """
        Return a cached yfinance option chain for a specific expiry.

        Args:
            symbol: Ticker symbol.
            expiry: Expiry date string (YYYY-MM-DD).

        Returns:
            yfinance OptionChain namedtuple with .calls and .puts DataFrames,
            or None on failure.
        """
        cache_key = f"{symbol}_{expiry}"
        now       = datetime.now()
        cached_ts = self._chain_ts.get(cache_key)
        if cached_ts and (now - cached_ts).total_seconds() < self._CACHE_TTL_SECONDS:
            return self._chain_cache.get(cache_key)

        try:
            ticker = yf.Ticker(symbol)
            chain  = ticker.option_chain(expiry)
            self._chain_cache[cache_key] = chain
            self._chain_ts[cache_key]    = now
            return chain
        except Exception as exc:
            log.warning("get_chain failed %s %s: %s", symbol, expiry, exc)
            return None

    def get_available_expirations(self, symbol: str) -> list[str]:
        """
        Return sorted list of available option expiration dates for a symbol.

        Args:
            symbol: Ticker symbol.

        Returns:
            List of date strings (YYYY-MM-DD), newest-first, or empty list on failure.
        """
        try:
            ticker = yf.Ticker(symbol)
            exps   = ticker.options
            return list(exps) if exps else []
        except Exception as exc:
            log.debug("get_available_expirations failed %s: %s", symbol, exc)
            return []

    def get_atm_iv(self, symbol: str) -> Optional[float]:
        """
        Return the ATM call implied volatility from the nearest qualifying expiry.

        Used as a fast IV check without running the full IVAnalyzer.

        Args:
            symbol: Ticker symbol.

        Returns:
            ATM IV as annualized fraction (e.g. 0.25 = 25%), or None if unavailable.
        """
        try:
            ticker      = yf.Ticker(symbol)
            expirations = ticker.options
            if not expirations:
                return None

            today      = date.today()
            qualifying = [
                e for e in expirations
                if 7 <= (date.fromisoformat(e) - today).days <= 45
            ]
            if not qualifying:
                return None

            exp   = qualifying[0]
            chain = ticker.option_chain(exp)
            calls = chain.calls
            if calls is None or calls.empty:
                return None

            hist = ticker.history(period="1d")
            if hist is None or hist.empty:
                return None
            spot = float(hist["Close"].iloc[-1])

            calls["strike_dist"] = (calls["strike"] - spot).abs()
            atm = calls.sort_values("strike_dist").iloc[0]
            iv  = float(atm.get("impliedVolatility", 0))
            return iv if iv > 0.01 else None

        except Exception as exc:
            log.debug("get_atm_iv failed %s: %s", symbol, exc)
            return None

    # ── Public: flow interpretation helpers ──────────────────────────────────

    @staticmethod
    def flow_is_bullish(flow: dict) -> bool:
        """Return True when flow data indicates net bullish institutional positioning."""
        if not flow:
            return False
        signal = flow.get("signal", "neutral")
        return signal in ("unusual_calls", "bullish_flow")

    @staticmethod
    def flow_is_bearish(flow: dict) -> bool:
        """Return True when flow data indicates net bearish institutional positioning."""
        if not flow:
            return False
        signal = flow.get("signal", "neutral")
        return signal in ("unusual_puts", "bearish_flow")

    @staticmethod
    def flow_is_high_conviction(flow: dict) -> bool:
        """Return True when flow data shows both unusual volume AND directional skew."""
        return bool(flow and flow.get("high_conviction"))
