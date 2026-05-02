"""
Insider buying detector using yfinance (sourced from SEC EDGAR Form 4 filings).
Open-market purchases by directors and officers are a strong conviction signal.
Results cached for 6 hours — EDGAR filings update once or twice daily.
"""
import yfinance as yf
from datetime import datetime, date, timedelta
from core.database import log
import config


class InsiderFlowClient:
    """Insider buying detector backed by yfinance SEC EDGAR Form 4 data.

    Checks each symbol for recent open-market purchases by directors and
    officers within a configurable lookback window. Only net purchases are
    flagged — sales and option exercises are filtered out.

    Cache TTL is 6 hours — EDGAR Form 4 filings update once or twice daily.
    """

    _CACHE_TTL_SECONDS = 21600  # 6 hours

    def __init__(self) -> None:
        """Initialize the insider flow client with an empty cache."""
        self._cache: dict[str, dict] = {}
        self._cache_ts: datetime | None = None

    def get_recent_insider_buys(self, symbols: list[str], days_back: int = 7,
                                 max_symbols: int = 50) -> dict[str, dict]:
        """Check each symbol for recent insider open-market purchases via yfinance.

        Args:
            symbols: List of ticker symbols to check.
            days_back: Number of calendar days to look back for insider purchases.
            max_symbols: Maximum number of symbols to fetch data for.

        Returns:
            A dict mapping symbol to a dict containing:
                insider_buying   -- True (always set when entry is present)
                buyer            -- name of the most recent insider buyer
                shares           -- shares purchased in that transaction
                value_usd        -- approximate USD value of that transaction
                days_ago         -- how many days ago the most recent purchase occurred
                total_purchases  -- total number of qualifying purchase transactions found
            Only net purchases within days_back calendar days are flagged.
            Symbols with no recent insider buying are absent from the result.
        """
        now = datetime.now()
        if (self._cache_ts is not None and
                (now - self._cache_ts).total_seconds() < self._CACHE_TTL_SECONDS):
            return {s: self._cache[s] for s in symbols if s in self._cache}

        result: dict[str, dict] = {}
        cutoff = date.today() - timedelta(days=days_back)
        # ETFs have no insider transactions on Yahoo Finance — skip to avoid 404 spam
        candidates = [s for s in symbols[:max_symbols]
                      if config.SYMBOL_BUCKET.get(s) != "index_etf"]

        for sym in candidates:
            try:
                ticker = yf.Ticker(sym)
                # insider_purchases: DataFrame with columns Insider Trading, Shares, Date, etc.
                df = getattr(ticker, "insider_purchases", None)
                if df is None or df.empty:
                    # Fall back to insider_transactions filtered for purchases
                    df = getattr(ticker, "insider_transactions", None)
                if df is None or df.empty:
                    continue

                recent_buys: list[dict] = []
                for _, row in df.iterrows():
                    try:
                        # Date column varies by yfinance version
                        tx_date_raw = (row.get("Date") or row.get("Start Date")
                                       or row.get("Transaction Date"))
                        if tx_date_raw is None:
                            continue
                        if hasattr(tx_date_raw, "date"):
                            tx_date = tx_date_raw.date()
                        else:
                            tx_date = date.fromisoformat(str(tx_date_raw)[:10])
                        if tx_date < cutoff:
                            continue

                        shares = float(row.get("Shares") or 0)
                        value  = float(row.get("Value")  or 0)
                        if shares <= 0:
                            continue

                        # Filter to purchases only when transaction type is available
                        tx_type = str(row.get("Transaction") or row.get("Text") or "").lower()
                        if tx_type and "purchase" not in tx_type and "buy" not in tx_type and "p-" not in tx_type:
                            continue

                        insider_name = str(
                            row.get("Insider Trading") or row.get("Insider") or "Unknown"
                        )[:60]

                        recent_buys.append({
                            "buyer":    insider_name,
                            "shares":   int(shares),
                            "value":    int(value),
                            "days_ago": (date.today() - tx_date).days,
                        })
                    except Exception:
                        continue

                if recent_buys:
                    best = min(recent_buys, key=lambda x: x["days_ago"])
                    result[sym] = {
                        "insider_buying":  True,
                        "buyer":           best["buyer"],
                        "shares":          best["shares"],
                        "value_usd":       best["value"],
                        "days_ago":        best["days_ago"],
                        "total_purchases": len(recent_buys),
                    }
            except Exception as e:
                log.debug("Insider flow failed %s: %s", sym, e)

        self._cache    = result
        self._cache_ts = now
        log.info("Insider buying: %d/%d symbols show recent purchases",
                 len(result), len(symbols[:max_symbols]))
        return {s: result[s] for s in symbols if s in result}
