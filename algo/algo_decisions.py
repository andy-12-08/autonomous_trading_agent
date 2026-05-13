"""
Options decision engine: three-engine architecture for determining which options
strategies to enter on each scan cycle.

Engine priority order:
  1. 0DTE Engine     — trending SPY/QQQ day within the entry window (highest priority)
  2. Premium Seller  — IV Rank ≥ 50%: iron condor or credit spread
  3. Directional Debit — IV Rank ≤ 30%: debit call/put spread with strong signal

Each engine runs independently and produces a recommendation dict or SKIP.
Recommendations flow to OptionsRiskManager for final approval before execution.

Hard-veto instruments are blocked before any engine runs: leveraged/inverse ETFs
and micro-caps below $10 have execution characteristics that break options sizing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import config
from analysis.options_strategy_selector import OptionsStrategySelector, SKIP
from core.database import log

# ── Instrument blocklist ──────────────────────────────────────────────────────
# Options on these instruments have structural problems: wide bid-ask spreads on
# leveraged ETFs reset nightly, and inverse ETFs move against our models.
_BLOCKED = frozenset({
    "SOXS", "SPXS", "SPXU", "SDOW", "SQQQ", "TZA", "SRTY", "ERY",
    "DRIP", "KOLD", "VXX", "UVXY", "SVXY", "DRV", "REK",
    "SOXL", "TQQQ", "UDOW", "URTY", "TNA", "ERX", "GUSH", "BOIL",
    "SPXL", "LABD", "LABU", "TECL", "TECS", "FAS", "FAZ",
    "BITO", "IBIT", "MSTU", "MSTX", "FBTC", "ARKB", "EZBC", "HODL",
})


class OptionsDecisionEngine:
    """
    Pure algorithmic, three-engine decision system for options strategies.

    Produces decision dicts consumed by the OptionsExecutorMixin.  Each dict
    contains:
      action          – 'ENTER', 'SKIP', or 'HOLD'
      symbol          – underlying ticker
      strategy_type   – one of the strategy label constants (e.g. 'credit_put_spread')
      direction       – 'bullish', 'bearish', or 'neutral'
      target_dte      – target days to expiry at entry
      spread_width    – width of spread in dollars
      short_delta     – target delta for the short leg
      long_delta      – target delta for the long leg
      max_premium_risk – maximum premium to risk on the trade
      rationale       – human-readable explanation of the strategy selection
      signal_score    – underlying signal score (0–10)
      iv_rank         – IV Rank at decision time
      iv_regime       – 'high', 'neutral', or 'low'
      vrp             – volatility risk premium
      atm_iv          – ATM implied volatility at decision time
      market_regime   – intraday market regime string

    SKIP decisions include only action, symbol, and rationale.
    HOLD decisions are emitted for each symbol already in an open position.
    """

    def __init__(self) -> None:
        """Initialize with a strategy selector instance."""
        self._selector = OptionsStrategySelector()

    def make_decisions(
        self,
        candidates:     list[dict],
        open_positions: list[dict],
        iv_data_map:    dict[str, dict],
        market_regime:  str,
        spy_move_pct:   float,
        vix_level:      float,
        hour:           int,
        minute:         int,
    ) -> list[dict]:
        """
        Evaluate all candidates through the three-engine architecture.

        Args:
            candidates:     Enriched candidate dicts from the scanner/enrichment
                            pipeline.  Each must contain at minimum: symbol,
                            signal_score, indicators, setup_type_hint.
            open_positions: List of open options position dicts from the database.
                            These are passed back as HOLD decisions so the audit
                            trail is complete every cycle.
            iv_data_map:    Output of IVAnalyzer.get_bulk_iv_regimes(), keyed by
                            symbol.  Symbols without IV data are skipped.
            market_regime:  Current intraday regime: 'trending_up',
                            'trending_down', 'ranging', or 'choppy'.
            spy_move_pct:   SPY move from today's open in percent.
            vix_level:      Current VIX or realized vol proxy.
            hour:           Current ET hour (0–23).
            minute:         Current ET minute (0–59).

        Returns:
            List of decision dicts (ENTER, SKIP, HOLD) for the executor.
        """
        decisions: list[dict] = []

        # ── HOLD all open positions ───────────────────────────────────────────
        # Position monitoring (50% profit, DTE exit, delta exit) is handled by
        # OptionsPositionMixin — we only emit HOLDs here for the audit trail.
        open_syms = set()
        for pos in open_positions:
            sym = pos.get("symbol", "")
            open_syms.add(sym)
            decisions.append({
                "action":   "HOLD",
                "symbol":   sym,
                "rationale": "Open position monitored by position manager",
            })

        # ── Sort candidates by signal score (strongest first) ─────────────────
        ranked = sorted(
            candidates,
            key=lambda x: float(x.get("signal_score", 0)),
            reverse=True,
        )

        enters_this_cycle = 0

        for item in ranked:
            sym   = item.get("symbol", "")
            score = float(item.get("signal_score", 0))

            if sym in open_syms:
                continue   # already in a position; position manager handles it

            # ── Hard instrument veto ─────────────────────────────────────────
            if sym in _BLOCKED:
                decisions.append(_skip(sym,
                    "Blocked instrument: options on leveraged/inverse ETFs have "
                    "structural bid-ask and reset issues"))
                continue

            # ── IV data required for all engines ─────────────────────────────
            iv_data = iv_data_map.get(sym)
            if not iv_data:
                decisions.append(_skip(sym,
                    "No IV data available — cannot evaluate engine suitability"))
                continue

            # ── Minimum price floor ───────────────────────────────────────────
            spot = float(item.get("indicators", {}).get("price", 0) or 0)
            if spot < config.MIN_UNDERLYING_PRICE:
                decisions.append(_skip(sym,
                    f"Underlying price ${spot:.2f} < minimum "
                    f"${config.MIN_UNDERLYING_PRICE:.2f} — options too cheap for "
                    "reliable delta targeting"))
                continue

            # ── Earnings blackout for credit strategies ───────────────────────
            has_earnings = bool(item.get("earnings_soon", False))

            # ── Strategy selector: routes through all three engines ───────────
            direction = _derive_direction(item)
            rec = self._selector.select_strategy(
                symbol            = sym,
                iv_data           = iv_data,
                signal_score      = score,
                signal_direction  = direction,
                market_regime     = market_regime,
                spy_move_pct      = spy_move_pct,
                vix_level         = vix_level,
                hour              = hour,
                minute            = minute,
                has_earnings_soon = has_earnings,
            )

            if rec["strategy_type"] == SKIP:
                decisions.append(_skip(sym, rec["rationale"]))
                continue

            # ── Enrichment veto: dark pool + options flow contradictions ──────
            veto, veto_reason = _enrichment_veto(item, rec, score)
            if veto:
                decisions.append(_skip(sym, veto_reason))
                continue

            # ── Cycle cap: don't enter more than MAX_OPTIONS_ENTRIES per scan ─
            if enters_this_cycle >= config.MAX_OPTIONS_ENTRIES_PER_CYCLE:
                decisions.append(_skip(sym,
                    f"Cycle entry cap ({config.MAX_OPTIONS_ENTRIES_PER_CYCLE}) reached "
                    f"— {sym} deferred to next cycle"))
                continue

            # ── Build ENTER decision dict ─────────────────────────────────────
            decisions.append({
                "action":           "ENTER",
                "symbol":           sym,
                "strategy_type":    rec["strategy_type"],
                "direction":        rec["direction"],
                "target_dte":       rec["target_dte"],
                "spread_width":     rec["spread_width"],
                "short_delta":      rec["short_delta"],
                "long_delta":       rec["long_delta"],
                "max_premium_risk": rec["max_premium_risk"],
                "rationale":        rec["rationale"],
                "signal_score":     score,
                "iv_rank":          iv_data.get("iv_rank",    50.0),
                "iv_regime":        iv_data.get("iv_regime",  "neutral"),
                "vrp":              iv_data.get("vrp",        0.0),
                "atm_iv":           iv_data.get("atm_iv",     0.20),
                "market_regime":    market_regime,
                "spot_price":       spot,
            })
            enters_this_cycle += 1

        _log_summary(decisions)
        return decisions

    # ── Convenience re-scorer ─────────────────────────────────────────────────

    @staticmethod
    def score_to_confidence(score: float) -> int:
        """Map a 0–10 signal score to a 1–10 integer confidence level.

        Args:
            score: Composite signal score.

        Returns:
            Integer confidence level consumed by risk sizing.
        """
        if score >= 9.5:
            return 10
        if score >= 8.5:
            return 9
        if score >= 7.5:
            return 8
        if score >= 6.5:
            return 7
        return 6


# ── Module-level helpers ──────────────────────────────────────────────────────

def _skip(symbol: str, reason: str) -> dict:
    """Build a minimal SKIP decision dict."""
    return {"action": "SKIP", "symbol": symbol, "rationale": reason}


def _derive_direction(item: dict) -> str:
    """
    Infer directional bias from the candidate's indicator snapshot.

    Uses three binary votes (EMA cross, VWAP position, MACD histogram sign)
    combined with the setup type hint to produce a clear directional label.
    Tied votes (1–1 or missing data) return 'neutral'.
    """
    sig        = item.get("indicators", {})
    setup      = item.get("setup_type_hint", "momentum")
    above_vwap = bool(sig.get("above_vwap", False))
    ema9       = float(sig.get("ema9",  0))
    ema21      = float(sig.get("ema21", 0))
    macd_hist  = float(sig.get("macd_hist", 0))

    bullish_votes = sum([above_vwap, ema9 > ema21, macd_hist > 0])

    if setup in ("gap_and_go", "momentum", "vwap_reclaim"):
        if bullish_votes >= 2:
            return "bullish"
        if bullish_votes == 0:
            return "bearish"
    elif setup == "mean_reversion":
        return "bullish" if above_vwap else "neutral"

    if bullish_votes >= 2:
        return "bullish"
    if bullish_votes == 0:
        return "bearish"
    return "neutral"


def _enrichment_veto(item: dict, rec: dict, score: float) -> tuple[bool, str]:
    """
    Block an ENTER decision when enrichment data directly contradicts the
    proposed strategy direction.

    Vetoes (direction-specific):
      - Credit put spread / bullish debit: dark pool distribution signal
      - Credit call spread / bearish debit: dark pool accumulation with strong upside
      - Either spread direction: options flow strongly contradicts the trade

    Args:
        item:  Enriched candidate dict.
        rec:   Strategy recommendation dict from OptionsStrategySelector.
        score: Underlying signal score (high scores reduce veto sensitivity).

    Returns:
        (True, veto_reason) if veto applies; (False, "") otherwise.
    """
    strategy = rec.get("strategy_type", "")
    direction = rec.get("direction", "neutral")

    # Dark pool distribution against a bullish strategy
    dp     = item.get("dark_pool", {})
    dp_sig = str(dp.get("signal", "")).lower() if isinstance(dp, dict) else ""
    dp_pct = float(dp.get("dark_pool_pct", 50)) if isinstance(dp, dict) else 50.0

    if direction == "bullish" and dp_sig == "distribution" and dp_pct < 35 and score < 8.5:
        return True, (
            f"Dark pool distribution ({dp_pct:.0f}% dark activity) contradicts "
            f"bullish {strategy} — institutional selling into retail momentum"
        )

    # Options flow contradiction: strong inverse flow vs the strategy direction
    opts      = item.get("options_flow", {})
    if isinstance(opts, dict):
        pc_ratio     = float(opts.get("put_call_ratio", 1.0))
        unusual_puts = bool(opts.get("unusual_puts",   False))
        unusual_calls= bool(opts.get("unusual_calls",  False))

        # Bearish flow against a bullish credit put spread
        if (direction == "bullish" and pc_ratio > 2.0 and unusual_puts
                and not item.get("has_catalyst") and score < 8.0):
            return True, (
                f"Options flow bearish (P/C={pc_ratio:.1f}, unusual puts) while "
                f"entering bullish {strategy} — smart money positioned against trade"
            )

        # Bullish flow against a bearish credit call spread
        if (direction == "bearish" and pc_ratio < 0.5 and unusual_calls
                and not item.get("has_catalyst") and score < 8.0):
            return True, (
                f"Options flow bullish (P/C={pc_ratio:.1f}, unusual calls) while "
                f"entering bearish {strategy} — flow contradicts short call thesis"
            )

    return False, ""


def _log_summary(decisions: list[dict]) -> None:
    """Log a one-line summary of the decision cycle results."""
    enter = sum(1 for d in decisions if d["action"] == "ENTER")
    hold  = sum(1 for d in decisions if d["action"] == "HOLD")
    skip  = sum(1 for d in decisions if d["action"] == "SKIP")
    log.info("Options decisions: %d ENTER  %d HOLD  %d SKIP", enter, hold, skip)
    for d in decisions:
        if d["action"] == "ENTER":
            log.info("  ENTER %-6s %-22s | %s",
                     d["symbol"], d["strategy_type"], d["rationale"][:80])
