"""
FINRA REGSHO daily short-sale volume — dark pool proxy.

FINRA legally requires all member firms to report short-sale volume daily.
The CNMS file aggregates this across all venues (NYSE TRF, NASDAQ TRF, OTC).

Why short volume predicts direction (counter-intuitive but empirically solid):
  HIGH short_vol_pct (>55%) = market makers shorting to fill institutional BUY orders.
    Institutions buy large blocks; the market maker takes the short side to provide
    liquidity, then closes out intraday. Net effect = bullish accumulation signal.
  LOW short_vol_pct (<35%)  = retail-driven buying with no institutional backstop,
    or quiet distribution by institutions into retail demand.
  NEUTRAL (35–55%)          = no directional information.

This is the same signal Squeeze Metrics commercializes as "DIX" (Dark Index).
The underlying data is free, public, and updated daily by 6 PM ET.

File URL: https://cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt
Format  : Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market
"""
import requests
from datetime import date, timedelta
from core.database import log


class DarkPoolClient:
    """FINRA REGSHO daily short-sale volume client (dark pool proxy).

    Downloads and parses the daily CNMS file from the FINRA CDN to produce
    per-symbol accumulation/distribution/neutral signals.

    The CNMS file is published by 6 PM ET each trading day. This client tries
    today's file first, then falls back up to 4 trading days to handle
    weekends and publication delays.

    IMPORTANT: the base directory URL returns 403 — only the full filename URL
    works. e.g. https://cdn.finra.org/equity/regsho/daily/CNMSshvol20260430.txt
    """

    # IMPORTANT: the base directory URL returns 403 — only the full filename URL works.
    _FINRA_CDN    = "https://cdn.finra.org/equity/regsho/daily"
    _FETCH_RETRIES = 3

    # Signal thresholds (from empirical analysis of DIX behaviour)
    ACCUMULATION_THRESHOLD = 0.55   # short% >= 55% → MM shorting to fill institutional buys
    DISTRIBUTION_THRESHOLD = 0.35   # short% <= 35% → retail-led or institutional selling

    def __init__(self) -> None:
        """Initialize the dark pool client with an empty cache."""
        self._cache: dict[str, dict] = {}
        self._cache_date: str = ""

    @staticmethod
    def _last_trading_day() -> date:
        """Return the most recent weekday on or before today.

        Returns:
            The most recent weekday date.
        """
        d = date.today()
        while d.weekday() >= 5:   # 5=Saturday, 6=Sunday
            d -= timedelta(days=1)
        return d

    def _fetch_finra_file(self, trading_date: date) -> dict[str, dict] | None:
        """Download and parse the FINRA CNMS short-sale file for a given date.

        The CDN hosts individual dated files — the base directory URL returns 403
        (directory listing disabled). Only the full filename URL works:
          GOOD: cdn.finra.org/equity/regsho/daily/CNMSshvol20260430.txt
          BAD:  cdn.finra.org/equity/regsho/daily  (403 Forbidden)

        Args:
            trading_date: The trading date whose CNMS file should be downloaded.

        Returns:
            A dict mapping symbol to {short_vol_pct, short_volume, total_volume},
            or None if the file is unavailable or a network error occurs.
        """
        filename = f"CNMSshvol{trading_date.strftime('%Y%m%d')}.txt"
        url      = f"{self._FINRA_CDN}/{filename}"
        r        = None

        for attempt in range(1, self._FETCH_RETRIES + 1):
            try:
                r = requests.get(url, timeout=15, headers={"User-Agent": "TradingBot/1.0"})
                if r.status_code == 200:
                    break
                if r.status_code in (403, 404):
                    # FINRA returns 403 (not 404) when today's file isn't published yet.
                    # Either way: file doesn't exist, no point retrying.
                    log.debug("Dark pool: %s not yet published (HTTP %s)", trading_date, r.status_code)
                    return None
                log.warning("Dark pool: HTTP %s for %s (attempt %d/%d)",
                            r.status_code, filename, attempt, self._FETCH_RETRIES)
                r = None
            except Exception as e:
                log.warning("Dark pool: network error fetching %s (attempt %d): %s",
                            filename, attempt, e)
                r = None

        if r is None:
            return None

        result: dict[str, dict] = {}
        lines = r.text.strip().split("\n")

        for line in lines[1:]:           # skip header
            line = line.strip()
            if not line or line.startswith("Date"):
                continue
            parts = line.split("|")
            if len(parts) < 5:
                continue
            try:
                symbol    = parts[1].strip().upper()
                short_vol = float(parts[2])
                total_vol = float(parts[4])
                if total_vol <= 0 or not symbol or len(symbol) > 5 or not symbol.isalpha():
                    continue
                short_pct = short_vol / total_vol
                if short_pct >= self.ACCUMULATION_THRESHOLD:
                    signal = "accumulation"   # bullish: institutions buying via MMs
                elif short_pct <= self.DISTRIBUTION_THRESHOLD:
                    signal = "distribution"   # bearish: low MM shorting = no big buyers
                else:
                    signal = "neutral"
                result[symbol] = {
                    "short_vol_pct": round(short_pct, 4),
                    "short_volume":  int(short_vol),
                    "total_volume":  int(total_vol),
                    "signal":        signal,
                    "date":          trading_date.isoformat(),
                }
            except Exception:
                continue

        log.info("Dark pool: parsed %d symbols from FINRA file (%s)", len(result), trading_date)
        return result

    def load_dark_pool_data(self, force_refresh: bool = False) -> dict[str, dict]:
        """Return today's full dark pool dataset {symbol: data}.

        Tries today's file first; falls back to the previous trading day if not
        yet published. Result is cached for the session — only one HTTP download
        per day.

        Args:
            force_refresh: If True, bypass the date-keyed cache and re-download.

        Returns:
            A dict mapping symbol to dark pool data, or an empty dict if no
            FINRA file could be found in the last 4 trading days.
        """
        today = self._last_trading_day().isoformat()
        if not force_refresh and self._cache and self._cache_date == today:
            return self._cache

        # Try today first, then yesterday (file published ~6 PM ET)
        for days_back in range(0, 4):
            candidate = self._last_trading_day() - timedelta(days=days_back)
            if candidate.weekday() >= 5:
                continue
            data = self._fetch_finra_file(candidate)
            if data:
                self._cache      = data
                self._cache_date = today
                return self._cache

        log.warning("Dark pool: no FINRA file found in last 4 trading days — running without")
        return {}

    def get_dark_pool_signals(self, symbols: list[str]) -> dict[str, dict]:
        """Return dark pool data for a specific list of symbols.

        Loads the full dataset on first call (cached for the day).

        Args:
            symbols: List of ticker symbols to look up.

        Returns:
            A dict mapping symbol to {short_vol_pct, short_volume, total_volume,
            signal, date}. Symbols with no FINRA data are absent from the result.
        """
        full = self.load_dark_pool_data()
        return {s: full[s] for s in symbols if s in full}

    def dark_pool_summary(self, symbols: list[str]) -> str:
        """Return a one-line summary of top accumulators and distributors.

        Args:
            symbols: List of ticker symbols to summarize.

        Returns:
            A human-readable string listing accumulation and distribution names.
        """
        data = self.get_dark_pool_signals(symbols)
        if not data:
            return "dark pool: no data"

        accum = sorted(
            [(s, d) for s, d in data.items() if d["signal"] == "accumulation"],
            key=lambda x: x[1]["short_vol_pct"], reverse=True,
        )[:5]
        distr = sorted(
            [(s, d) for s, d in data.items() if d["signal"] == "distribution"],
            key=lambda x: x[1]["short_vol_pct"],
        )[:5]

        parts = []
        if accum:
            parts.append("accumulation: " + ", ".join(
                f"{s}({d['short_vol_pct']:.0%})" for s, d in accum))
        if distr:
            parts.append("distribution: " + ", ".join(
                f"{s}({d['short_vol_pct']:.0%})" for s, d in distr))
        return "dark pool — " + " | ".join(parts) if parts else "dark pool: all neutral"
