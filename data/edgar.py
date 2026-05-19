"""
SEC EDGAR real-time 8-K filing gate.

8-K filings are material event disclosures: earnings, FDA decisions, M&A, CFO changes,
analyst restatements. A bad 8-K can drop a stock 1020% in minutes. A good one can
spike it  but a technical setup that looks perfect can be building toward a known
negative that insiders are selling ahead of.

We check EDGAR's full-text search API (no auth required, free) before executing any BUY.
If a fresh 8-K was filed within the last LOOKBACK_HOURS for the target symbol, the trade
is vetoed  the event invalidates the technical thesis.

URL: https://efts.sec.gov/LATEST/search-index?q=%22AAPL%22&forms=8-K&dateRange=custom&startdt=YYYY-MM-DD&enddt=YYYY-MM-DD
"""
import requests
from datetime import date, datetime, timezone, timedelta
from core.database import log


class EdgarClient:
    """SEC EDGAR real-time 8-K filing gate.

    Checks EDGAR's full-text search API (no auth required, free) for fresh 8-K
    filings before any BUY order is executed. A same-day 8-K vetoes the trade
    because the material event invalidates the technical thesis.

    Cache TTL is 15 minutes per symbol  8-K data is near-realtime but one
    check per scan cycle is sufficient.
    """

    _BASE    = "https://efts.sec.gov/LATEST/search-index"
    _TIMEOUT = 6

    LOOKBACK_HOURS     = 2     # veto if 8-K filed in the last 2 hours
    _CACHE_TTL_SECONDS = 900   # 15 min  one check per cycle is enough

    def __init__(self) -> None:
        """Initialize the EDGAR client with an empty per-symbol cache."""
        # {sym: (has_8k, reason, cached_at)}
        self._cache: dict[str, tuple[bool, str, datetime]] = {}

    def check_fresh_8k(self, symbol: str) -> tuple[bool, str]:
        """Check if a fresh 8-K was filed for this symbol in the last LOOKBACK_HOURS.

        Args:
            symbol: The ticker symbol to check against EDGAR.

        Returns:
            A tuple (veto, reason) where:
                veto=True  -- do NOT enter; material event exists that invalidates the setup
                veto=False -- no disqualifying 8-K found; proceed normally
        """
        now = datetime.now(timezone.utc)

        # Cache hit
        if symbol in self._cache:
            has_8k, reason, cached_at = self._cache[symbol]
            if (now - cached_at).total_seconds() < self._CACHE_TTL_SECONDS:
                return has_8k, reason

        result = self._fetch_8k(symbol, now)
        self._cache[symbol] = (result[0], result[1], now)
        return result

    def _fetch_8k(self, symbol: str, now: datetime) -> tuple[bool, str]:
        """Query the EDGAR full-text search API for same-day 8-K filings.

        Args:
            symbol: The ticker symbol to search for.
            now: The current UTC datetime (used to calculate the recency window).

        Returns:
            A tuple (veto, reason) describing whether a disqualifying filing
            was found and a human-readable explanation.
        """
        today = date.today().isoformat()
        try:
            resp = requests.get(
                self._BASE,
                params={
                    "q":         f'"{symbol}"',
                    "forms":     "8-K",
                    "dateRange": "custom",
                    "startdt":   today,
                    "enddt":     today,
                },
                timeout=self._TIMEOUT,
                headers={"User-Agent": "TradingBot/1.0 research@example.com"},
            )
            if resp.status_code != 200:
                log.debug("EDGAR: HTTP %d for %s  allowing trade", resp.status_code, symbol)
                return False, "EDGAR unreachable  allowing"

            hits = resp.json().get("hits", {}).get("hits", [])
            if not hits:
                return False, "no 8-K today"

            # Check if any filing is within LOOKBACK_HOURS
            cutoff = now - timedelta(hours=self.LOOKBACK_HOURS)
            fresh = []
            for hit in hits:
                filed_str = (hit.get("_source") or {}).get("file_date", "")
                try:
                    filed_dt = datetime.fromisoformat(filed_str + "T00:00:00+00:00")
                    # EDGAR date is day-level; treat any same-day 8-K as potentially recent
                    if filed_dt.date() >= cutoff.date():
                        title = (hit.get("_source") or {}).get("period_of_report", filed_str)
                        fresh.append(title)
                except Exception:
                    fresh.append("unknown date")

            if fresh:
                reason = f"Fresh 8-K filing today for {symbol}  material event risk: {fresh[0]}"
                log.warning("EDGAR 8-K veto: %s", reason)
                return True, reason

            return False, f"8-K exists but outside {self.LOOKBACK_HOURS}h window"

        except Exception as e:
            log.debug("EDGAR: error checking %s: %s  allowing trade", symbol, e)
            return False, "EDGAR error  allowing"
