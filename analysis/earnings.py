"""
Earnings blackout and correlation-guard methods for MarketGuard.
"""
from datetime import date, timedelta
import pandas as pd
import config
from core.database import log


class EarningsMixin:
    """Mixin providing earnings blackout detection and position correlation checks."""

    def reset_earnings_cache(self):
        """
        Clear the earnings blackout cache so each day starts fresh.

        Call at the daily reset  earnings dates can change overnight.
        """
        self._earnings_cache = {}

    def is_earnings_blackout(self, symbol: str) -> tuple[bool, str]:
        """
        Check whether a symbol is in its earnings blackout window.

        Uses yfinance to look up the next earnings date. Cached per session
        to avoid repeated API calls.

        Args:
            symbol: Ticker symbol to check.

        Returns:
            Tuple of (blackout: bool, reason: str). Fail-open: if data is
            unavailable, returns (False, ...) to allow the trade.
        """
        if symbol in self._earnings_cache:
            return self._earnings_cache[symbol]

        try:
            import yfinance as yf
            import requests as _req
            _sess = _req.Session()
            _sess.request = lambda method, url, **kw: _req.Session.request(  # type: ignore[method-assign]
                _sess, method, url, timeout=kw.pop("timeout", 10), **kw
            )
            ticker = yf.Ticker(symbol, session=_sess)
            today  = date.today()
            cutoff = today + timedelta(days=config.EARNINGS_BLACKOUT_DAYS)

            # yfinance = 0.2: calendar is a dict or DataFrame
            cal = ticker.calendar
            if cal is None:
                result = (False, "no earnings data")
                self._earnings_cache[symbol] = result
                return result

            # Collect candidate earnings dates from dict or DataFrame
            dates_to_check: list = []
            if isinstance(cal, dict):
                for key in ("Earnings Date", "earnings_date", "earningsDate"):
                    val = cal.get(key)
                    if val is None:
                        continue
                    if isinstance(val, (list, tuple)):
                        dates_to_check.extend(val)
                    else:
                        dates_to_check.append(val)
            elif hasattr(cal, "columns"):
                for col in cal.columns:
                    if "earnings" in str(col).lower():
                        dates_to_check.extend(cal[col].dropna().tolist())

            for raw_date in dates_to_check:
                try:
                    ts = pd.Timestamp(raw_date)
                    if ts is pd.NaT or str(ts) == "NaT":
                        continue
                    ed: date = ts.date()  # type: ignore[assignment]
                    if today <= ed <= cutoff:
                        result = (True, (f"EARNINGS BLACKOUT: {symbol} reports on {ed} "
                                         f" binary gap risk, skip until after announcement"))
                        self._earnings_cache[symbol] = result
                        log.warning(result[1])
                        return result
                except Exception:
                    continue

            result = (False, f"{symbol}: no earnings within {config.EARNINGS_BLACKOUT_DAYS}-day window")
            self._earnings_cache[symbol] = result
            return result

        except Exception as e:
            result = (False, f"earnings check unavailable for {symbol}: {e}")
            self._earnings_cache[symbol] = result
            return result

    # -- 3. Correlation Guard ---------------------------------------------------

    def check_correlation(self, symbol: str, open_positions: list[dict]) -> tuple[bool, str]:
        """
        Check whether adding a new position would create excessive correlation.

        Fetches 10-day daily returns for both the candidate and each existing holding.
        Blocks entry if Pearson correlation with any holding exceeds
        config.MAX_HOLDING_CORRELATION.

        This prevents holding AAPL + MSFT + NVDA simultaneously  all crash together.

        Args:
            symbol:         Ticker symbol of the candidate new position.
            open_positions: List of current position dicts, each with a "symbol" key.

        Returns:
            Tuple of (allowed: bool, reason: str). Fail-open: insufficient data
            or API failure returns (True, ...) to allow the trade.
        """
        if not open_positions:
            return True, "no existing positions  correlation check skipped"

        try:
            cand_df = self.broker.get_bars(symbol, "1Day", days=14)
            if cand_df.empty or len(cand_df) < 5:
                return True, f"insufficient data for {symbol} correlation check (fail-open)"

            cand_ret = cand_df["close"].pct_change().dropna()

            for pos in open_positions:
                pos_sym = pos["symbol"]
                if pos_sym == symbol:
                    continue
                try:
                    pos_df = self.broker.get_bars(pos_sym, "1Day", days=14)
                    if pos_df.empty or len(pos_df) < 5:
                        continue

                    pos_ret = pos_df["close"].pct_change().dropna()
                    aligned = pd.DataFrame({"c": cand_ret, "p": pos_ret}).dropna()

                    if len(aligned) < 5:
                        continue

                    # Use numpy directly to avoid Pyright confusion with pandas .corr()
                    import numpy as np
                    c_vals = aligned["c"].to_numpy(dtype=float)
                    p_vals = aligned["p"].to_numpy(dtype=float)
                    corr_matrix = np.corrcoef(c_vals, p_vals)
                    corr = float(corr_matrix[0, 1])
                    if corr > config.MAX_HOLDING_CORRELATION:
                        reason = (f"CORRELATION GUARD: {symbol} vs {pos_sym} r={corr:.2f} "
                                  f"> {config.MAX_HOLDING_CORRELATION:.2f}  "
                                  f"concentrated factor bet, diversify to uncorrelated sector")
                        log.info(reason)
                        return False, reason

                except Exception:
                    continue

            return True, f"correlation OK  {symbol} sufficiently uncorrelated with open positions"

        except Exception as e:
            log.warning("Correlation check failed for %s: %s  fail-open", symbol, e)
            return True, f"correlation check failed (fail-open): {e}"
