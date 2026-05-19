"""
Yield curve + credit spread macro signal  free data via yfinance.

Two inputs:
  1. Treasury yield spread (10Y minus 3M):
       > +1.0%  = normal / healthy  accommodative to equities
       0 to 1%  = cautious  flattening signals slowing growth
       < 0%     = inverted  historically precedes recessions 618 months out

  2. Credit spread proxy (HYG vs LQD intraday relative performance):
       HYG = iShares iBoxx High Yield Bond ETF (junk / risk-on)
       LQD = iShares iBoxx Investment Grade Bond ETF (safer / risk-off)
       HYG outperforming LQD = risk appetite healthy (credit bullish)
       HYG underperforming LQD = credit stress emerging (risk-off signal)

Risk signal and size multiplier:
  "risk_on"  -> 1.00  (normal sizing)
  "normal"   -> 1.00
  "cautious" -> 0.85  (trim size 15%)
  "risk_off" -> 0.75  (trim size 25%)

Cache: 1 hour (intraday bond/yield data changes slowly; one refresh per hour is enough).
"""
import yfinance as yf
import pandas as pd
from datetime import datetime, timezone
from core.database import log


class YieldCurveClient:
    """Yield curve and credit spread macro signal using free yfinance data.

    Two inputs:
      1. Treasury yield spread (10Y minus 3M): inverted = recession risk
      2. Credit spread proxy (HYG vs LQD): HYG outperform = risk appetite healthy

    Results cached for 1 hour  bond/yield data changes slowly intraday.
    """

    _CACHE_TTL_SECONDS = 3600    # 1 hour

    # Yield spread thresholds
    SPREAD_RISK_OFF   = 0.0    # 10Y-3M < 0.0 means inverted
    SPREAD_CAUTIOUS   = 1.0    # 10Y-3M < 1.0 means flattening
    # Credit stress: HYG underperforms LQD by this much intraday
    CREDIT_STRESS_PCT = -0.30  # HYG change - LQD change < -0.30% means stress

    def __init__(self) -> None:
        """Initialize the yield curve client with an empty cache."""
        self._cache: dict | None = None
        self._cache_ts: datetime | None = None

    def get_yield_curve(self, force_refresh: bool = False) -> dict:
        """Return current yield curve + credit spread signal.

        Args:
            force_refresh: If True, bypass the cache and fetch fresh data.

        Returns:
            A dict containing:
                ten_year_yield    -- ^TNX current yield (%)
                three_month_yield -- ^IRX current yield (%)
                spread_10y_3m     -- 10Y - 3M spread (%)
                hyg_change_pct    -- HYG intraday % change
                lqd_change_pct    -- LQD intraday % change
                credit_delta      -- HYG change - LQD change (positive = risk appetite healthy)
                signal            -- "risk_on" | "normal" | "cautious" | "risk_off"
                size_multiplier   -- float to apply alongside VIX factor in position sizing
                note              -- one-line human readable summary
            On failure returns a safe default ("normal", 1.0).
        """
        now = datetime.now(timezone.utc)
        if (not force_refresh and self._cache is not None and self._cache_ts is not None and
                (now - self._cache_ts).total_seconds() < self._CACHE_TTL_SECONDS):
            return self._cache

        result = self._fetch_yield_curve()
        self._cache    = result
        self._cache_ts = now
        return result

    def _fetch_yield_curve(self) -> dict:
        """Download yield and credit data from yfinance and compute the macro signal.

        Returns:
            A dict with yield curve fields and signal classification.
            Returns safe defaults if data is unavailable.
        """
        default = {
            "ten_year_yield":    None,
            "three_month_yield": None,
            "spread_10y_3m":     None,
            "hyg_change_pct":    None,
            "lqd_change_pct":    None,
            "credit_delta":      None,
            "signal":            "normal",
            "size_multiplier":   1.0,
            "note":              "yield curve: data unavailable  using normal sizing",
        }

        try:
            tickers = yf.download(
                tickers=["^TNX", "^IRX", "HYG", "LQD"],
                period="2d",
                interval="1d",
                group_by="ticker",
                auto_adjust=True,
                progress=False,
                threads=True,
            )

            if tickers is None or tickers.empty:
                log.warning("Yield curve: download returned empty data")
                return default

            multi = isinstance(tickers.columns, pd.MultiIndex)

            def _series(sym: str) -> "pd.Series | None":
                """Return non-null close series for a downloaded symbol.

                Args:
                    sym: Yahoo Finance ticker in the combined response.

                Returns:
                    Close-price series, or None when unavailable.
                """
                try:
                    if multi:
                        df = tickers[sym]  # type: ignore[index]
                    else:
                        df = tickers
                    closes = pd.Series(df["Close"]).dropna()
                    return closes if not closes.empty else None
                except Exception:
                    return None

            def get_close(sym: str) -> float | None:
                """Return the latest close for a symbol.

                Args:
                    sym: Yahoo Finance ticker.

                Returns:
                    Latest close, or None when missing.
                """
                s = _series(sym)
                return float(s.iloc[-1]) if s is not None else None  # type: ignore[union-attr]

            def get_prev_close(sym: str) -> float | None:
                """Return the prior close for a symbol.

                Args:
                    sym: Yahoo Finance ticker.

                Returns:
                    Prior close, or None when fewer than two closes exist.
                """
                s = _series(sym)
                return float(s.iloc[-2]) if s is not None and len(s) >= 2 else None  # type: ignore[union-attr]

            tnx  = get_close("^TNX")
            irx  = get_close("^IRX")
            hyg  = get_close("HYG")
            lqd  = get_close("LQD")
            hyg0 = get_prev_close("HYG")
            lqd0 = get_prev_close("LQD")

            if tnx is None or irx is None:
                log.warning("Yield curve: could not fetch TNX/IRX  using normal default")
                return default

            spread = round(tnx - irx, 3)

            hyg_chg = round((hyg - hyg0) / hyg0 * 100, 3) if (hyg and hyg0 and hyg0 > 0) else None
            lqd_chg = round((lqd - lqd0) / lqd0 * 100, 3) if (lqd and lqd0 and lqd0 > 0) else None
            credit_delta = round(hyg_chg - lqd_chg, 3) if (hyg_chg is not None and lqd_chg is not None) else None

            # Classify signal
            if spread < self.SPREAD_RISK_OFF:
                signal = "risk_off"
                mult   = 0.75
            elif spread < self.SPREAD_CAUTIOUS:
                signal = "cautious"
                mult   = 0.85
            else:
                signal = "normal"
                mult   = 1.0

            # Credit stress can escalate cautious to risk_off.
            if credit_delta is not None and credit_delta < self.CREDIT_STRESS_PCT:
                if signal == "normal":
                    signal = "cautious"
                    mult   = 0.85
                elif signal == "cautious":
                    signal = "risk_off"
                    mult   = 0.75

            note = (
                f"10Y={tnx:.2f}% 3M={irx:.2f}% spread={spread:+.2f}% "
                f"| HYG{hyg_chg:+.2f}% LQD{lqd_chg:+.2f}% credit_delta={credit_delta:+.2f}%"
                if (hyg_chg is not None and lqd_chg is not None)
                else f"10Y={tnx:.2f}% 3M={irx:.2f}% spread={spread:+.2f}%"
            )

            result = {
                "ten_year_yield":    round(tnx, 3),
                "three_month_yield": round(irx, 3),
                "spread_10y_3m":     spread,
                "hyg_change_pct":    hyg_chg,
                "lqd_change_pct":    lqd_chg,
                "credit_delta":      credit_delta,
                "signal":            signal,
                "size_multiplier":   mult,
                "note":              note,
            }
            log.info("Yield curve: %s [%s %.2f]", note, signal.upper(), mult)
            return result

        except Exception as e:
            log.warning("Yield curve: fetch error  %s. Using normal default.", e)
            return default
