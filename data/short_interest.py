"""
Short interest data via yfinance ticker.info  free, bi-monthly cadence.

Short interest = the number of shares currently held SHORT by all market participants.
Published by FINRA/NASDAQ twice a month (mid-month and end-of-month settlement dates).

Why it matters:
  HIGH short interest (>20% of float) = large pool of short sellers who MUST buy to close.
    When positive catalyst hits, forced covering amplifies upside (short squeeze).
    Also signals institutional conviction that the stock will fall  respect the signal.
  HIGH days-to-cover (>10 days) = squeeze fuel. If positive flow hits, covering lasts days.
  LOW short interest (<5%) = institutions not positioned bearish. No squeeze fuel.

Data fields from yfinance.Ticker.info:
  shortPercentOfFloat  ? decimal (0.20 = 20%)
  shortRatio           ? days to cover (shares_short / avg_daily_volume)
  sharesShort          ? raw count of shares short
  sharesFloat          ? total float shares

Cache: 12 hours. The underlying data updates only twice a month so hourly refreshes
  are wasteful; once per half-session is more than sufficient.
"""
import yfinance as yf
from datetime import datetime, timezone
from core.database import log
import config


class ShortInterestClient:
    """Short interest data client backed by yfinance ticker.info.

    Fetches FINRA/NASDAQ bi-monthly short interest data including short percent
    of float, days-to-cover, and raw shares short. Classifies each symbol into
    one of four signals: squeeze_risk, elevated, normal, or low.

    Cache TTL is 12 hours  the underlying data updates only twice a month.
    """

    _CACHE_TTL_SECONDS = 43200   # 12 hours

    HIGH_SHORT_INTEREST = 0.20   # >= 20% float short = squeeze candidate
    HIGH_DAYS_TO_COVER  = 10.0   # >= 10 days to cover = significant squeeze fuel
    LOW_SHORT_INTEREST  = 0.05   # <=  5% = no meaningful short overhang

    def __init__(self) -> None:
        """Initialize the short interest client with an empty cache."""
        self._cache: dict[str, dict] = {}
        self._cache_ts: datetime | None = None

    def get_short_interest(self, symbols: list[str], force_refresh: bool = False,
                           max_symbols: int = 50) -> dict[str, dict]:
        """Return short interest data for each symbol.

        Args:
            symbols: List of ticker symbols to check.
            force_refresh: If True, bypass the cache and fetch fresh data.
            max_symbols: Maximum number of symbols to fetch in one call.

        Returns:
            A dict mapping symbol to a dict containing:
                short_pct_float -- % of float that is short (0.01.0 scale)
                days_to_cover   -- short interest / avg daily volume
                shares_short    -- raw number of shares short
                signal          -- "squeeze_risk" | "elevated" | "normal" | "low"
                note            -- human-readable one-liner
            Symbols with no data are absent from the result.
        """
        now = datetime.now(timezone.utc)
        if (not force_refresh and self._cache and self._cache_ts is not None and
                (now - self._cache_ts).total_seconds() < self._CACHE_TTL_SECONDS):
            return {s: self._cache[s] for s in symbols if s in self._cache}

        result: dict[str, dict] = {}
        # ETFs have no short-float fundamentals on Yahoo Finance  skip to avoid 404 spam
        candidates = [s for s in symbols[:max_symbols]
                      if config.SYMBOL_BUCKET.get(s) != "index_etf"]

        for sym in candidates:
            try:
                info  = yf.Ticker(sym).info
                pct   = info.get("shortPercentOfFloat") or 0.0
                ratio = info.get("shortRatio")          or 0.0
                raw   = info.get("sharesShort")         or 0

                pct   = float(pct)
                ratio = float(ratio)

                if pct >= self.HIGH_SHORT_INTEREST and ratio >= self.HIGH_DAYS_TO_COVER:
                    signal = "squeeze_risk"
                    note   = f"{pct:.0%} float short, {ratio:.1f}d cover  high squeeze risk"
                elif pct >= self.HIGH_SHORT_INTEREST:
                    signal = "elevated"
                    note   = f"{pct:.0%} float short  elevated short interest"
                elif pct <= self.LOW_SHORT_INTEREST:
                    signal = "low"
                    note   = f"{pct:.0%} float short  minimal short overhang"
                else:
                    signal = "normal"
                    note   = f"{pct:.0%} float short, {ratio:.1f}d cover"

                result[sym] = {
                    "short_pct_float": round(pct, 4),
                    "days_to_cover":   round(ratio, 2),
                    "shares_short":    int(raw),
                    "signal":          signal,
                    "note":            note,
                }
            except Exception:
                pass  # yfinance 404 for ETFs/missing data  suppressed by logger config

        self._cache.update(result)
        self._cache_ts = now

        squeeze = [s for s, d in result.items() if d["signal"] == "squeeze_risk"]
        log.info("Short interest: fetched %d/%d | squeeze candidates: %s",
                 len(result), len(candidates), squeeze or "none")
        return {s: result[s] for s in symbols if s in result}

    def short_interest_summary(self, symbols: list[str]) -> str:
        """Return a one-line summary of the highest short interest names.

        Args:
            symbols: List of ticker symbols to summarize.

        Returns:
            A human-readable string listing the top five short interest names.
        """
        data = self.get_short_interest(symbols)
        if not data:
            return "short interest: no data"
        top = sorted(data.items(), key=lambda x: x[1]["short_pct_float"], reverse=True)[:5]
        return "short interest  " + ", ".join(
            f"{s}({d['short_pct_float']:.0%} [{d['signal']}])" for s, d in top
        )
