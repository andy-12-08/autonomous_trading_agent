"""
Options strategy selector: maps IV regime, market conditions, and underlying
signal quality to the optimal strategy for the current environment.

This is the brain of the three-engine architecture.  It does NOT execute
orders — it returns a recommendation dict that the executor consumes.

Strategy selection logic:

  IV Rank ≥ 50% (expensive IV)  → Sell premium (iron condor or credit spread)
  IV Rank ≤ 30% (cheap IV)      → Buy debit spread (only with strong signal)
  Trending market day + 0DTE    → 0DTE SPX call/put spread
  IV between 30–50%             → Skip options, trade the underlying stock instead

Key principle: never fight the IV regime.  Buying options when IV is high
destroys edge.  Selling options when IV is low leaves money on the table.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

import config
from core.database import log


# ── Strategy type labels ──────────────────────────────────────────────────────
CREDIT_PUT_SPREAD  = "credit_put_spread"
CREDIT_CALL_SPREAD = "credit_call_spread"
IRON_CONDOR        = "iron_condor"
DEBIT_CALL_SPREAD  = "debit_call_spread"
DEBIT_PUT_SPREAD   = "debit_put_spread"
ZERO_DTE_CALL      = "zero_dte_call_spread"
ZERO_DTE_PUT       = "zero_dte_put_spread"
SKIP               = "skip"


class OptionsStrategySelector:
    """
    Selects the optimal options strategy for a symbol based on IV regime,
    market structure, underlying signal quality, and time of day.

    Each strategy recommendation contains all parameters the executor needs
    to construct and submit the actual orders.
    """

    def select_strategy(
        self,
        symbol:           str,
        iv_data:          dict,
        signal_score:     float,
        signal_direction: str,
        market_regime:    str,
        spy_move_pct:     float,
        vix_level:        float,
        hour:             int,
        minute:           int,
        has_earnings_soon: bool = False,
    ) -> dict:
        """
        Select the best options strategy for the current conditions.

        Args:
            symbol:            Ticker symbol.
            iv_data:           Output of IVAnalyzer.get_iv_data().
            signal_score:      Underlying directional signal score (0–10).
            signal_direction:  'bullish', 'bearish', or 'neutral'.
            market_regime:     'trending_up', 'trending_down', 'ranging', 'choppy'.
            spy_move_pct:      SPY move from today's open in percent.
            vix_level:         Current VIX or realized vol proxy.
            hour:              Current ET hour.
            minute:            Current ET minute.
            has_earnings_soon: True if earnings within EARNINGS_BLACKOUT_DAYS_CREDIT days.

        Returns:
            Strategy recommendation dict containing:
              strategy_type  – one of the strategy label constants above
              symbol         – the symbol to trade options on
              direction      – 'bullish', 'bearish', or 'neutral'
              target_dte     – target days to expiry
              spread_width   – width of spread in dollars
              short_delta    – target delta for the short leg
              long_delta     – target delta for the long leg
              max_premium_risk – maximum dollars to risk on this trade
              rationale      – human-readable explanation
            strategy_type == 'skip' means no option trade is recommended.
        """
        iv_regime  = iv_data.get("iv_regime", "unknown")
        iv_rank    = iv_data.get("iv_rank",   50.0)
        atm_iv     = iv_data.get("atm_iv",    0.20)
        vrp        = iv_data.get("vrp",       0.0)
        bid_ask_ok = iv_data.get("bid_ask_ok", True)

        cur_min      = hour * 60 + minute
        open_min     = config.MARKET_OPEN_HOUR * 60 + config.MARKET_OPEN_MIN
        prime_end    = config.PRIME_ENTRY_END_HOUR * 60 + config.PRIME_ENTRY_END_MIN
        close_min    = config.MARKET_CLOSE_HOUR * 60 + config.MARKET_CLOSE_MIN
        zte_start    = config.ZERO_DTE_ENTRY_START_HOUR * 60 + config.ZERO_DTE_ENTRY_START_MIN
        zte_end      = config.ZERO_DTE_ENTRY_END_HOUR   * 60 + config.ZERO_DTE_ENTRY_END_MIN

        # ── Hard gates ────────────────────────────────────────────────────────
        if cur_min >= close_min:
            return self._skip(symbol, "Late-day gate: no new entries after 3:45 PM ET")

        if atm_iv < config.MIN_ATM_IV:
            return self._skip(symbol, f"ATM IV {atm_iv:.1%} too low — thin premium, bad fills")

        if not bid_ask_ok:
            return self._skip(symbol, "Option bid-ask spread too wide — fill quality unacceptable")

        # ── 0DTE check (highest priority on qualifying days) ──────────────────
        if self._qualifies_for_zero_dte(symbol, spy_move_pct, vix_level, cur_min, zte_start, zte_end):
            direction = "bullish" if spy_move_pct > 0 else "bearish"
            strategy  = ZERO_DTE_CALL if direction == "bullish" else ZERO_DTE_PUT
            return {
                "strategy_type":    strategy,
                "symbol":           symbol,
                "direction":        direction,
                "target_dte":       0,
                "spread_width":     config.ZERO_DTE_SPREAD_WIDTH,
                "short_delta":      config.ZERO_DTE_SHORT_DELTA,
                "long_delta":       config.ZERO_DTE_TARGET_DELTA,
                "max_premium_risk": config.MAX_PREMIUM_PER_TRADE * 0.67,   # smaller size for 0DTE
                "rationale":        (
                    f"0DTE {direction}: SPY {spy_move_pct:+.2f}% from open, "
                    f"VIX={vix_level:.1f}, trending market"
                ),
            }

        # ── Engine 1: Premium Seller (IV Rank ≥ 50%) ─────────────────────────
        if iv_regime == "high":
            if has_earnings_soon:
                return self._skip(symbol,
                    f"Premium seller skipped: earnings within "
                    f"{config.EARNINGS_BLACKOUT_DAYS_CREDIT} days (IV crush timing risk)")

            if vrp < config.MIN_VRP_TO_SELL:
                return self._skip(symbol,
                    f"VRP {vrp:.1f} pts below minimum {config.MIN_VRP_TO_SELL} — "
                    "no statistical edge for sellers at this level")

            # Choose iron condor for index ETFs in ranging markets;
            # credit spread on individual stocks or trending markets
            if symbol in config.PREMIUM_SELLER_SYMBOLS and market_regime == "ranging":
                return {
                    "strategy_type":    IRON_CONDOR,
                    "symbol":           symbol,
                    "direction":        "neutral",
                    "target_dte":       30,   # 30 DTE sweet spot for condors
                    "spread_width":     config.IRON_CONDOR_WING_WIDTH,
                    "short_delta":      config.IRON_CONDOR_TARGET_DELTA,
                    "long_delta":       config.IRON_CONDOR_TARGET_DELTA * 0.5,
                    "max_premium_risk": config.MAX_PREMIUM_PER_TRADE,
                    "rationale":        (
                        f"Iron condor: IV Rank {iv_rank:.0f}, VRP +{vrp:.1f} pts, "
                        f"ranging market — sell both wings"
                    ),
                }
            else:
                # Directional credit spread: sell put spread if bullish/neutral,
                # sell call spread if bearish
                if signal_direction in ("bullish", "neutral"):
                    strategy  = CREDIT_PUT_SPREAD
                    direction = "bullish"
                    rationale = f"Credit put spread: IV Rank {iv_rank:.0f}, VRP +{vrp:.1f} pts, market {market_regime}"
                else:
                    strategy  = CREDIT_CALL_SPREAD
                    direction = "bearish"
                    rationale = f"Credit call spread: IV Rank {iv_rank:.0f}, VRP +{vrp:.1f} pts, bearish bias"

                return {
                    "strategy_type":    strategy,
                    "symbol":           symbol,
                    "direction":        direction,
                    "target_dte":       21,
                    "spread_width":     config.CREDIT_SPREAD_WIDTH,
                    "short_delta":      config.CREDIT_SPREAD_TARGET_DELTA,
                    "long_delta":       config.CREDIT_SPREAD_TARGET_DELTA * 0.5,
                    "max_premium_risk": config.MAX_PREMIUM_PER_TRADE,
                    "rationale":        rationale,
                }

        # ── Engine 2: Directional Debit (IV Rank ≤ 30%) ──────────────────────
        if iv_regime == "low":
            # Only enter debit trades with a strong underlying signal
            if signal_score < config.DEBIT_MIN_SIGNAL_SCORE:
                return self._skip(symbol,
                    f"Debit entry skipped: signal {signal_score:.1f} < "
                    f"minimum {config.DEBIT_MIN_SIGNAL_SCORE} required for debit trades")

            if signal_direction == "neutral":
                return self._skip(symbol,
                    "Debit entry skipped: neutral signal — debit spreads need directional conviction")

            # Outside the prime entry window, require even higher conviction
            if cur_min > prime_end and signal_score < config.DEBIT_MIN_SIGNAL_SCORE + 1.0:
                return self._skip(symbol,
                    f"Debit entry skipped: outside prime window and score "
                    f"{signal_score:.1f} < {config.DEBIT_MIN_SIGNAL_SCORE + 1:.1f} required")

            if signal_direction == "bullish":
                strategy = DEBIT_CALL_SPREAD
                rationale = (
                    f"Debit call spread: IV Rank {iv_rank:.0f} (cheap premium), "
                    f"score {signal_score:.1f}, bullish signal"
                )
            else:
                strategy = DEBIT_PUT_SPREAD
                rationale = (
                    f"Debit put spread: IV Rank {iv_rank:.0f} (cheap premium), "
                    f"score {signal_score:.1f}, bearish signal"
                )

            return {
                "strategy_type":    strategy,
                "symbol":           symbol,
                "direction":        signal_direction,
                "target_dte":       14,
                "spread_width":     config.CREDIT_SPREAD_WIDTH,    # same width as credit
                "short_delta":      config.DEBIT_SHORT_DELTA,      # sell the OTM hedge
                "long_delta":       config.DEBIT_TARGET_DELTA,     # buy ATM
                "max_premium_risk": config.MAX_PREMIUM_PER_TRADE,
                "rationale":        rationale,
            }

        # ── IV neutral (30–50%): no options edge ─────────────────────────────
        return self._skip(symbol,
            f"IV Rank {iv_rank:.0f} is neutral (30–50%) — no statistical edge "
            "for buyers or sellers; trade the underlying instead")

    def select_strategy_batch(
        self,
        candidates:    list[dict],
        iv_data_map:   dict[str, dict],
        market_regime: str,
        spy_move_pct:  float,
        vix_level:     float,
        hour:          int,
        minute:        int,
    ) -> list[dict]:
        """
        Select strategies for a list of candidate symbols in one pass.

        Args:
            candidates:    List of watchlist candidate dicts with signal fields.
            iv_data_map:   Output of IVAnalyzer.get_bulk_iv_regimes().
            market_regime: Current intraday regime string.
            spy_move_pct:  SPY move from open in percent.
            vix_level:     Current VIX or realized vol proxy.
            hour:          Current ET hour.
            minute:        Current ET minute.

        Returns:
            List of strategy recommendation dicts (strategy_type != 'skip' only).
        """
        strategies = []
        for item in candidates:
            symbol    = item.get("symbol", "")
            iv_data   = iv_data_map.get(symbol, {})
            if not iv_data:
                continue

            score     = float(item.get("signal_score", 0))
            direction = self._derive_direction(item)
            earnings  = bool(item.get("earnings_soon", False))

            rec = self.select_strategy(
                symbol=symbol,
                iv_data=iv_data,
                signal_score=score,
                signal_direction=direction,
                market_regime=market_regime,
                spy_move_pct=spy_move_pct,
                vix_level=vix_level,
                hour=hour,
                minute=minute,
                has_earnings_soon=earnings,
            )
            if rec["strategy_type"] != SKIP:
                strategies.append(rec)
                log.info("Strategy selected: %-20s %-6s %s", symbol, rec["strategy_type"], rec["rationale"][:80])

        log.info("Strategy selector: %d/%d candidates have actionable strategies",
                 len(strategies), len(candidates))
        return strategies

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _skip(symbol: str, reason: str) -> dict:
        """Return a SKIP recommendation with the reason for logging."""
        return {
            "strategy_type":    SKIP,
            "symbol":           symbol,
            "direction":        "neutral",
            "target_dte":       0,
            "spread_width":     0.0,
            "short_delta":      0.0,
            "long_delta":       0.0,
            "max_premium_risk": 0.0,
            "rationale":        reason,
        }

    @staticmethod
    def _qualifies_for_zero_dte(
        symbol:       str,
        spy_move_pct: float,
        vix_level:    float,
        cur_min:      int,
        zte_start:    int,
        zte_end:      int,
    ) -> bool:
        """Return True if 0DTE conditions are met."""
        if symbol not in config.ZERO_DTE_SYMBOLS:
            return False
        if vix_level > config.ZERO_DTE_MAX_VIX:
            return False
        if abs(spy_move_pct) < config.ZERO_DTE_MIN_SPY_MOVE_PCT:
            return False
        if not (zte_start <= cur_min <= zte_end):
            return False
        return True

    @staticmethod
    def _derive_direction(item: dict) -> str:
        """Infer directional bias from signal fields in a candidate dict."""
        sig   = item.get("indicators", {})
        score = float(item.get("signal_score", 5.0))
        setup = item.get("setup_type_hint", "momentum")

        above_vwap = bool(sig.get("above_vwap", False))
        ema_bull   = float(sig.get("ema9",  0)) > float(sig.get("ema21", 0))
        macd_hist  = float(sig.get("macd_hist", 0))

        bullish_votes = sum([above_vwap, ema_bull, macd_hist > 0])

        if setup in ("gap_and_go", "momentum", "vwap_reclaim"):
            if bullish_votes >= 2:
                return "bullish"
            elif bullish_votes <= 0:
                return "bearish"
        elif setup == "mean_reversion":
            return "bullish" if above_vwap else "neutral"

        if bullish_votes >= 2:
            return "bullish"
        elif bullish_votes == 0:
            return "bearish"
        return "neutral"
