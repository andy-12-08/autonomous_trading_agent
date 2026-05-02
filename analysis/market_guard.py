"""
Institutional-grade market-wide risk guards.

Three independent layers, all fail-open (if data unavailable, allow trade):

1. Circuit Breaker   — halts ALL new entries when broad market is in stress
                       (SPY down ≥ 1.5% from open OR UVXY up ≥ 5% intraday)

2. Earnings Blackout — blocks any symbol reporting earnings within 2 calendar days
                       (binary event risk — stop-losses cannot protect against gaps)

3. Correlation Guard — blocks a new position if its 10-day return correlation
                       with any existing holding exceeds 0.80
                       (prevents concentrated factor bets that all fail at once)
"""
import pandas as pd
from datetime import date, datetime, timedelta
import config

from core.database import log


from analysis.earnings import EarningsMixin


class MarketGuard(EarningsMixin):
    """
    Market-wide risk guard implementing circuit breakers, earnings blackouts,
    VIX regime detection, market structure analysis, intraday regime detection,
    and correlation checks.

    Args:
        broker:     Broker client instance with a get_bars() method.
        indicators: IndicatorEngine instance providing get_key_levels() and
                    compute_indicators() methods.
    """

    def __init__(self, broker, indicators):
        """
        Initialize MarketGuard with broker and indicator engine dependencies.

        Args:
            broker:     Broker client providing bar data access.
            indicators: IndicatorEngine instance for technical analysis.
        """
        self.broker = broker
        self.indicators = indicators

        # ── Circuit breaker state ──────────────────────────────────────────────
        self._circuit_broken: bool = False   # latch: once triggered, stays on for the day
        self._circuit_reason: str  = ""

        # ── Earnings cache ─────────────────────────────────────────────────────
        self._earnings_cache: dict = {}

        # ── VIX regime cache ───────────────────────────────────────────────────
        self._vix_cache = None
        self._vix_cache_ts = None
        self._VIX_TTL = 900  # seconds

        # ── Market structure cache ─────────────────────────────────────────────
        self._mkt_struct_cache = None
        self._mkt_struct_cache_ts = None
        self._MKT_STRUCT_TTL = 300   # 5-minute cache

        # ── Intraday regime cache ──────────────────────────────────────────────
        self._regime_cache = None
        self._regime_cache_ts = None
        self._REGIME_TTL = 600   # 10 minutes

    # ── 1. Market Circuit Breaker ─────────────────────────────────────────────

    def reset_circuit_breaker(self):
        """
        Reset the circuit breaker latch so each day starts clean.

        Call this at the daily reset before the session begins.
        """
        self._circuit_broken = False
        self._circuit_reason = ""

    def check_circuit_breaker(self) -> tuple[bool, str]:
        """
        Check whether broad market conditions allow new entries.

        Checks once per call; latches for the rest of the session once triggered.
        Triggers on:
          - SPY down ≥ CIRCUIT_BREAKER_SPY_DROP_PCT from today's first bar open
          - UVXY up  ≥ CIRCUIT_BREAKER_UVXY_SURGE_PCT from today's first bar open

        Returns:
            Tuple of (trading_allowed: bool, reason: str). When False, reason
            contains a human-readable explanation of what triggered the breaker.
        """
        if self._circuit_broken:
            return False, self._circuit_reason   # already tripped — stay off

        try:
            spy_df = self.broker.get_bars("SPY", "5Min", days=1)
            if not spy_df.empty and len(spy_df) >= 2:
                spy_open = float(spy_df["open"].iloc[0])
                spy_now  = float(spy_df["close"].iloc[-1])
                if spy_open > 0:
                    spy_chg = (spy_now - spy_open) / spy_open * 100
                    if spy_chg <= config.CIRCUIT_BREAKER_SPY_DROP_PCT:
                        self._circuit_broken = True
                        self._circuit_reason = (
                            f"CIRCUIT BREAKER: SPY {spy_chg:+.1f}% from open "
                            f"(threshold {config.CIRCUIT_BREAKER_SPY_DROP_PCT:.1f}%). "
                            f"Broad market sell-off — no new entries for the rest of the day."
                        )
                        log.warning(self._circuit_reason)
                        return False, self._circuit_reason
        except Exception as e:
            log.warning("Circuit breaker SPY check failed: %s", e)

        try:
            uvxy_df = self.broker.get_bars("UVXY", "5Min", days=1)
            if not uvxy_df.empty and len(uvxy_df) >= 2:
                uvxy_open = float(uvxy_df["open"].iloc[0])
                uvxy_now  = float(uvxy_df["close"].iloc[-1])
                if uvxy_open > 0:
                    uvxy_chg = (uvxy_now - uvxy_open) / uvxy_open * 100
                    if uvxy_chg >= config.CIRCUIT_BREAKER_UVXY_SURGE_PCT:
                        self._circuit_broken = True
                        self._circuit_reason = (
                            f"CIRCUIT BREAKER: UVXY +{uvxy_chg:.1f}% intraday "
                            f"(threshold +{config.CIRCUIT_BREAKER_UVXY_SURGE_PCT:.0f}%). "
                            f"Volatility spike — no new entries for the rest of the day."
                        )
                        log.warning(self._circuit_reason)
                        return False, self._circuit_reason
        except Exception as e:
            log.warning("Circuit breaker UVXY check failed: %s", e)

        return True, "circuit breaker OK"

    # ── 1b. VIX Regime ────────────────────────────────────────────────────────

    def get_vix_regime(self) -> tuple[str, float, float]:
        """
        Estimate market volatility regime using SPY's 10-day realized volatility.

        Uses SPY's 10-day realized volatility (annualized) as a market fear proxy.
        Cached for 15 minutes — one SPY daily-bar fetch per cycle is enough.

        Returns:
            Tuple of (label, realized_vol_pct, size_factor) where:
              label        : "calm" | "normal" | "elevated" | "extreme"
              realized_vol : annualized % (comparable to VIX scale)
              size_factor  : multiply calc_qty result by this before placing order
            Fail-open: returns ("normal", 0.0, 1.0) on any data error.
        """
        now = datetime.now()
        if (self._vix_cache is not None and self._vix_cache_ts is not None
                and (now - self._vix_cache_ts).total_seconds() < self._VIX_TTL):
            return self._vix_cache

        try:
            df = self.broker.get_bars("SPY", "1Day", days=15)
            if df.empty or len(df) < 5:
                return ("normal", 0.0, 1.0)

            rvol = float(df["close"].pct_change().dropna().std() * (252 ** 0.5) * 100)

            for threshold, factor, label in config.VIX_REGIME_THRESHOLDS:
                if rvol >= threshold:
                    result: tuple[str, float, float] = (label, round(rvol, 1), factor)
                    self._vix_cache    = result
                    self._vix_cache_ts = now
                    return result

            result = ("calm", round(rvol, 1), 1.10)
            self._vix_cache    = result
            self._vix_cache_ts = now
            return result

        except Exception as e:
            log.warning("VIX regime check failed: %s — defaulting to normal", e)
            return ("normal", 0.0, 1.0)

    # ── 1c. Market Structure (SPY / QQQ key levels) ───────────────────────────

    def get_market_structure(self) -> dict:
        """
        Compute SPY and QQQ key levels and derive a market_posture label.

        Cached for 5 minutes — structural levels don't shift bar-to-bar.

        Market posture values:
          "above_pdh"  — SPY broke above prev day high → broad bullish momentum, tailwind
          "near_pdh"   — SPY within 0.5% of prev day high → approaching resistance wall
          "mid_range"  — SPY between prev day levels → neutral, trade on individual signal
          "near_pdl"   — SPY within 0.5% of prev day low → approaching support, be cautious
          "below_pdl"  — SPY broke below prev day low → broad weakness, very selective

        Returns:
            Dict with SPY and QQQ price/level fields plus market_posture label.
            Fail-open: returns {} on any error so trading is never blocked.
        """
        now = datetime.now()
        if (self._mkt_struct_cache is not None and self._mkt_struct_cache_ts is not None
                and (now - self._mkt_struct_cache_ts).total_seconds() < self._MKT_STRUCT_TTL):
            return self._mkt_struct_cache

        result: dict = {}
        try:
            for ticker in ("SPY", "QQQ"):
                prefix   = ticker.lower()
                df_5m    = self.broker.get_bars(ticker, "5Min",  days=3)
                df_day   = self.broker.get_bars(ticker, "1Day",  days=10)
                if df_5m.empty:
                    continue
                levels = self.indicators.get_key_levels(df_5m, df_day if not df_day.empty else None)
                price  = float(df_5m["close"].iloc[-1])
                result[f"{prefix}_price"]          = round(price, 2)
                result[f"{prefix}_prev_day_high"]  = levels.get("prev_day_high")
                result[f"{prefix}_prev_day_low"]   = levels.get("prev_day_low")
                result[f"{prefix}_premarket_high"] = levels.get("premarket_high")
                result[f"{prefix}_premarket_low"]  = levels.get("premarket_low")
                result[f"{prefix}_nearest_res"]    = levels.get("nearest_resistance")
                result[f"{prefix}_nearest_sup"]    = levels.get("nearest_support")

            # Derive market posture from SPY relative to yesterday's structure
            spy_price = result.get("spy_price", 0.0) or 0.0
            spy_pdh   = result.get("spy_prev_day_high") or 0.0
            spy_pdl   = result.get("spy_prev_day_low")  or 0.0

            if spy_price > 0 and spy_pdh > 0 and spy_pdl > 0:
                vs_pdh = (spy_price - spy_pdh) / spy_pdh * 100
                vs_pdl = (spy_price - spy_pdl) / spy_pdl * 100
                if vs_pdh >= 0.10:
                    posture = "above_pdh"
                elif vs_pdh >= -0.50:
                    posture = "near_pdh"
                elif vs_pdl <= -0.10:
                    posture = "below_pdl"
                elif vs_pdl <= 0.50:
                    posture = "near_pdl"
                else:
                    posture = "mid_range"
                result["market_posture"] = posture
                result["spy_vs_pdh_pct"] = round(vs_pdh, 2)
                result["spy_vs_pdl_pct"] = round(vs_pdl, 2)
                log.info("Market structure: SPY=%.2f PDH=%.2f PDL=%.2f → posture=%s (vs_pdh=%+.2f%%)",
                         spy_price, spy_pdh, spy_pdl, posture, vs_pdh)

        except Exception as e:
            log.warning("get_market_structure failed: %s — fail-open", e)
            result = {}

        self._mkt_struct_cache    = result
        self._mkt_struct_cache_ts = now
        return result

    # ── 1d. Intraday Regime Detector ──────────────────────────────────────────

    def reset_intraday_regime(self):
        """
        Reset the intraday regime cache so each day starts without a stale label.

        Call at the daily reset before the session begins.
        """
        self._regime_cache    = None
        self._regime_cache_ts = None

    def get_intraday_regime(self) -> dict:
        """
        Classify today's market environment as trending, ranging, or choppy.

        Re-evaluated every 10 minutes using SPY 5-min bars. Uses three signals:
          1. ATR expansion ratio  — recent 5-bar ATR vs prior 15-bar ATR.
                                    Expansion > 1.2x = institutional activity.
          2. Directional consistency — fraction of last 10 bars moving the same way.
                                    High = trending; low = oscillating.
          3. Today's range vs ATR — if today's range < 40% of ATR → very tight = choppy.

        Fail-open: defaults to "ranging" (neutral) on any data error.

        Returns:
            Dict with keys:
              regime   : "trending" | "ranging" | "choppy"
              note     : human-readable summary for logging and account context
              atr_expansion_ratio, directional_strength, today_range_pct,
              up_bars_of_last_10  (raw metrics for transparency)
        """
        now = datetime.now()
        if (self._regime_cache is not None and self._regime_cache_ts is not None
                and (now - self._regime_cache_ts).total_seconds() < self._REGIME_TTL):
            return self._regime_cache

        default = {"regime": "ranging", "note": "default — no SPY data"}

        try:
            df = self.broker.get_bars("SPY", "5Min", days=2)
            if df.empty or len(df) < 20:
                self._regime_cache    = default
                self._regime_cache_ts = now
                return default

            df = self.indicators.compute_indicators(df)

            price = float(df["close"].iloc[-1])
            if price <= 0:
                self._regime_cache    = default
                self._regime_cache_ts = now
                return default

            # ── Today's session range ─────────────────────────────────────────
            dates      = [t.date() for t in df.index]
            today_date = dates[-1]
            today_bars = df[[d == today_date for d in dates]]
            if len(today_bars) < 4:
                self._regime_cache    = default
                self._regime_cache_ts = now
                return default

            today_range_pct = (float(today_bars["high"].max()) -
                               float(today_bars["low"].min())) / price * 100

            # ── ATR expansion: recent vs historical ───────────────────────────
            atr_recent = float(df["atr"].iloc[-5:].mean())
            atr_older  = float(df["atr"].iloc[-20:-5].mean()) if len(df) >= 20 else atr_recent
            atr_pct    = float(df["atr"].iloc[-1]) / price * 100
            atr_ratio  = atr_recent / atr_older if atr_older > 0 else 1.0

            # ── Directional consistency (last 10 bars) ────────────────────────
            closes    = df["close"].iloc[-10:].tolist()
            up_bars   = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1])
            direction = abs(up_bars - (len(closes) - 1 - up_bars)) / max(len(closes) - 1, 1)

            # ── Classification ────────────────────────────────────────────────
            if atr_ratio >= 1.2 and direction >= 0.5:
                regime = "trending"
                note   = (f"SPY trending — ATR ×{atr_ratio:.1f}, "
                          f"{up_bars}/{len(closes)-1} bars directional, "
                          f"range {today_range_pct:.2f}%")
            elif atr_ratio <= 0.8 and direction <= 0.3:
                regime = "choppy"
                note   = (f"SPY choppy — ATR ×{atr_ratio:.1f} (contracting), "
                          f"mixed direction {direction:.1f}, "
                          f"range {today_range_pct:.2f}%")
            elif today_range_pct < atr_pct * 0.4:
                regime = "choppy"
                note   = (f"SPY tight range {today_range_pct:.2f}% "
                          f"< 40% of ATR {atr_pct:.2f}% — low-conviction chop")
            else:
                regime = "ranging"
                note   = (f"SPY ranging — ATR ×{atr_ratio:.1f}, "
                          f"{up_bars}/{len(closes)-1} directional, "
                          f"range {today_range_pct:.2f}%")

            result = {
                "regime":               regime,
                "note":                 note,
                "atr_expansion_ratio":  round(atr_ratio,         2),
                "directional_strength": round(direction,          2),
                "today_range_pct":      round(today_range_pct,   2),
                "up_bars_of_last_10":   up_bars,
            }
        except Exception as e:
            log.warning("get_intraday_regime failed: %s — defaulting to ranging", e)
            result = {"regime": "ranging", "note": f"error: {e}"}

        self._regime_cache    = result
        self._regime_cache_ts = now
        return result

    # ── 2. Earnings Blackout ───────────────────────────────────────────────────

