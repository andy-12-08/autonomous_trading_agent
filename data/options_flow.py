"""
Options flow scanner using yfinance (free CBOE data via Yahoo Finance).
Detects unusual call activity — a leading indicator of institutional positioning.
Results cached for 30 minutes to avoid hammering the API on every scan cycle.
"""
import yfinance as yf
from datetime import datetime, date
from core.database import log


class OptionsFlowClient:
    """Options flow scanner using free yfinance CBOE data.

    Scans the nearest qualifying expiry for each symbol and classifies options
    activity as unusual_calls, bullish_flow, bearish_flow, or neutral based on
    put/call ratio and call volume vs open interest.

    Cache TTL is 30 minutes — options chains update continuously intraday but
    one snapshot per half-hour is sufficient for scan-cycle decisions.
    """

    _CACHE_TTL_SECONDS = 1800  # 30 minutes

    def __init__(self) -> None:
        """Initialize the options flow client with an empty cache."""
        self._cache: dict[str, dict] = {}
        self._cache_ts: datetime | None = None

    def get_options_flow(self, symbols: list[str], max_symbols: int = 30) -> dict[str, dict]:
        """Fetch nearest-expiry options chain for each symbol and detect unusual activity.

        Args:
            symbols: List of ticker symbols to scan.
            max_symbols: Maximum number of symbols to fetch options data for.

        Returns:
            A dict mapping symbol to a dict containing:
                unusual_calls   -- bool: call volume > 2× open interest today
                put_call_ratio  -- put volume / call volume
                call_volume     -- total call contracts traded today
                put_volume      -- total put contracts traded today
                signal          -- "unusual_calls" | "bullish_flow" | "bearish_flow" | "neutral"
                expiry          -- expiry date string used for the chain
            Symbols with no options data or thin markets (<100 total contracts)
            are absent from the result.
            Cache is warm for 30 min; first call per session is slow (~0.5s per symbol).
        """
        now = datetime.now()
        if (self._cache_ts is not None and
                (now - self._cache_ts).total_seconds() < self._CACHE_TTL_SECONDS):
            return {s: self._cache[s] for s in symbols if s in self._cache}

        result: dict[str, dict] = {}
        today = date.today()

        for sym in symbols[:max_symbols]:
            try:
                ticker      = yf.Ticker(sym)
                expirations = ticker.options
                if not expirations:
                    continue

                # Prefer expiries >=7 days out to avoid expiring-today noise
                qualifying = [e for e in expirations
                              if (date.fromisoformat(e) - today).days >= 7]
                exp   = qualifying[0] if qualifying else expirations[0]
                chain = ticker.option_chain(exp)
                calls = chain.calls
                puts  = chain.puts
                if calls.empty or puts.empty:
                    continue

                call_vol = float(calls["volume"].fillna(0).sum())
                put_vol  = float(puts["volume"].fillna(0).sum())
                call_oi  = float(calls["openInterest"].fillna(0).sum())

                if call_vol + put_vol < 100:  # too thin to be meaningful
                    continue

                pc_ratio      = put_vol / call_vol if call_vol > 0 else 9.99
                # Unusual: today's call volume > 2× open interest = net new institutional longs
                unusual_calls = bool(call_oi > 0 and call_vol > call_oi * 2.0)

                if unusual_calls:
                    signal = "unusual_calls"
                elif pc_ratio < 0.7:
                    signal = "bullish_flow"
                elif pc_ratio > 1.5:
                    signal = "bearish_flow"
                else:
                    signal = "neutral"

                result[sym] = {
                    "unusual_calls":  unusual_calls,
                    "put_call_ratio": round(pc_ratio, 2),
                    "call_volume":    int(call_vol),
                    "put_volume":     int(put_vol),
                    "signal":         signal,
                    "expiry":         exp,
                }
            except Exception as e:
                log.debug("Options flow failed %s: %s", sym, e)

        self._cache    = result
        self._cache_ts = now
        log.info("Options flow: %d/%d symbols have data", len(result), len(symbols[:max_symbols]))
        return {s: result[s] for s in symbols if s in result}
