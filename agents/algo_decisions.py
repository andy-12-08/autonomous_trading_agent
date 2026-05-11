"""
Algorithmic decision engine — replaces the LLM trading agent.

Decision flow per cycle:
  1. HOLD for all open positions (mechanical stops handle exits)
  2. Sort candidates by signal_score descending
  3. Hard instrument veto  (inverse / leveraged / crypto ETFs)
  4. Signal score floor    (< MIN_BUY_SCORE → SKIP)
  5. Volume profile gate   (POC, value area, LVN)
  6. Setup-type quality gate (momentum, gap_and_go, vwap_reclaim, mean_reversion)
  7. Enrichment signals gate (dark pool distribution, bearish options flow)
  8. Top MAX_BUYS by score → BUY; everything else → SKIP
"""

import config
from core.database import log

# ── Instrument blocklist ────────────────────────────────────────────────────────
# These instruments are structurally unsuitable for the algo sizing model:
# inverse ETFs move opposite to our long-only strategy, leveraged ETFs decay
# and have extreme ATR, crypto ETFs have non-equity risk characteristics.
BLOCKED_INSTRUMENTS = frozenset({
    # Inverse ETFs
    "SOXS", "SPXS", "SPXU", "SDOW", "SQQQ", "TZA", "SRTY", "ERY",
    "DRIP", "KOLD", "VXX", "UVXY", "SVXY", "DRV", "REK",
    # Leveraged long ETFs (excessive ATR blows up risk sizing)
    "SOXL", "TQQQ", "UDOW", "URTY", "TNA", "ERX", "GUSH", "BOIL",
    "SPXL", "LABD", "LABU", "TECL", "TECS", "FAS", "FAZ",
    # Crypto ETFs (non-equity risk, 24h market dynamics don't map to intraday)
    "BITO", "IBIT", "MSTU", "MSTX", "FBTC", "ARKB", "EZBC", "HODL",
})

# ── Score → confidence mapping ─────────────────────────────────────────────────
# Mirrors the executor's CONFIDENCE_SIZE_SCALE tiers — higher score = bigger size.
_SCORE_CONF = [
    (9.5, 9),   # maximum conviction
    (8.5, 8),   # strong
    (7.5, 7),   # solid
    (6.5, 6),   # minimum passing
]

MIN_BUY_SCORE = 7.0   # floor below which we never BUY
MAX_BUYS      = 5     # maximum BUY decisions per scan cycle


def _score_to_conf(score: float) -> int:
    """Map a composite signal score to an integer confidence level (6–9).

    Uses _SCORE_CONF tiers; returns 6 (minimum passing) when no tier matches.

    Args:
        score: Composite signal score from the signal scorer.

    Returns:
        Integer confidence in [6, 9] consumed by the executor's sizing logic.
    """
    for threshold, conf in _SCORE_CONF:
        if score >= threshold:
            return conf
    return 6


def _skip(sym: str, conf: int, reason: str) -> dict:
    """Build a minimal SKIP decision dict in the format execute_decisions expects.

    Args:
        sym: Uppercase ticker symbol.
        conf: Signal confidence integer (used for audit logging only).
        reason: Human-readable skip reason stored in the decisions table.

    Returns:
        Decision dict with action=SKIP and final_decision=SKIP.
    """
    return {
        "symbol":            sym,
        "action":            "SKIP",
        "final_decision":    "SKIP",
        "signal_confidence": conf,
        "reason_for_entry":  reason,
        "reason_to_avoid":   "",
    }


class AlgoDecisionEngine:
    """Pure algorithmic replacement for TradingAgent.ask_agent().

    Produces decision dicts in the same format as the executor expects,
    so execute_decisions() and all downstream risk guards work unchanged.
    """

    @staticmethod
    def make_decisions(
        candidates: list[dict],
        open_positions: list[dict],
    ) -> list[dict]:
        """Evaluate candidates and open positions; return BUY / SKIP / HOLD dicts.

        Args:
            candidates: Pre-filtered, enriched candidate dicts — any order.
            open_positions: Current open position snapshots from the broker.

        Returns:
            List of decision dicts consumable by execute_decisions().
        """
        decisions: list[dict] = []

        # ── HOLD all open positions ────────────────────────────────────────────
        # The position manager's mechanical stops are the real exit logic.
        # We emit HOLD so the executor records the decision for the audit trail.
        for pos in open_positions:
            decisions.append({
                "symbol":            pos["symbol"],
                "action":            "HOLD",
                "final_decision":    "HOLD",
                "signal_confidence": 7,
                "reason_for_entry":  "Algo: holding — mechanical stops active",
            })

        open_syms = {p["symbol"] for p in open_positions}

        # Re-sort by score descending so MAX_BUYS picks the strongest setups,
        # not the ones that happen to lead the sector-priority ordering.
        ranked = sorted(
            candidates,
            key=lambda x: float(x.get("signal_score", 0)),
            reverse=True,
        )

        buys = 0
        for item in ranked:
            sym   = item["symbol"]
            score = float(item.get("signal_score", 0))
            sig   = item.get("indicators", {})
            setup = item.get("setup_type_hint", "momentum")

            if sym in open_syms:
                continue

            # ── Hard instrument veto ───────────────────────────────────────────
            if sym in BLOCKED_INSTRUMENTS:
                decisions.append(_skip(sym, 2,
                    "Blocked: inverse/leveraged/crypto ETF — unsuitable for algo sizing"))
                continue

            # ── Signal score floor ─────────────────────────────────────────────
            if score < MIN_BUY_SCORE:
                decisions.append(_skip(sym, max(2, int(score)),
                    f"Score {score:.1f} below minimum {MIN_BUY_SCORE}"))
                continue

            # ── Volume profile gate ────────────────────────────────────────────
            vp_skip, vp_reason = _volume_profile_gate(sig, score)
            if vp_skip:
                decisions.append(_skip(sym, 4, vp_reason))
                continue

            # ── Setup-type quality gate ────────────────────────────────────────
            sq_skip, sq_reason = _setup_quality_gate(sig, setup, score)
            if sq_skip:
                decisions.append(_skip(sym, 5, sq_reason))
                continue

            # ── Enrichment signals gate ────────────────────────────────────────
            es_skip, es_reason = _enrichment_gate(item, score)
            if es_skip:
                decisions.append(_skip(sym, 5, es_reason))
                continue

            # ── Top-N cap ─────────────────────────────────────────────────────
            if buys >= MAX_BUYS:
                decisions.append(_skip(sym, 5,
                    f"Score {score:.1f} — outside top {MAX_BUYS} candidates this cycle"))
                continue

            conf     = _score_to_conf(score)
            evidence = " | ".join(item.get("signal_evidence", [])[:5])
            decisions.append({
                "symbol":            sym,
                "action":            "BUY",
                "final_decision":    "BUY",
                "signal_confidence": conf,
                "setup_type":        setup,
                "reason_for_entry":  (
                    f"Score {score:.1f} [{item.get('signal_class', '')}]; {evidence}"
                ),
                "reason_to_avoid":   "",
            })
            buys += 1

        buy_count  = sum(1 for d in decisions if d["action"] == "BUY")
        hold_count = sum(1 for d in decisions if d["action"] == "HOLD")
        skip_count = sum(1 for d in decisions if d["action"] == "SKIP")
        log.info("Algo decisions: %d BUY  %d HOLD  %d SKIP", buy_count, hold_count, skip_count)
        return decisions


# ── Gate implementations ────────────────────────────────────────────────────────

def _volume_profile_gate(sig: dict, score: float) -> tuple[bool, str]:
    """Gate on volume profile position relative to POC, value area, and LVN.

    Volume profile fields (poc, vah, val, in_value_area, above_value_area,
    below_value_area, near_poc, lvn_above) are computed by
    patterns.compute_volume_profile() and merged into sig by the scanner.

    Rules (in order):
      below VAL + bearish EMA → institutional supply rejected it → SKIP
      inside value area without escape velocity → auction chop → SKIP
        (exception: score ≥ 8.5 AND full EMA bull + above VWAP)
      near POC without very high score → mean-reversion magnet → SKIP
        (exception: score ≥ 9.0 OR already above VAH)
    """
    above_vwap = bool(sig.get("above_vwap"))
    ema9       = float(sig.get("ema9",  0))
    ema21      = float(sig.get("ema21", 0))
    ema_bull   = ema9 > ema21

    poc = sig.get("poc")
    vah = sig.get("vah")
    val = sig.get("val")

    # Below VAL — rejected from the volume cluster; overhead supply is dense
    if sig.get("below_value_area") and not ema_bull:
        val_str = f"VAL {float(val):.2f}" if val else "value area low"
        return True, f"Below {val_str} — rejected from volume support, institutional supply overhead"

    # Inside value area — price is in auction; momentum entries stall here because
    # the market is still discovering fair value between VAL and VAH.
    # Only allow if score signals breakout-in-progress (≥8.5) with full confirmation.
    if sig.get("in_value_area") and not sig.get("above_value_area"):
        if score < 8.5 or not (ema_bull and above_vwap):
            poc_str = f"POC {float(poc):.2f}" if poc else "value area"
            return True, (
                f"Inside value area ({poc_str}) — auction zone, "
                "momentum setups stall between VAL and VAH"
            )

    # Near POC — the highest-volume price acts as a gravitational centre.
    # Without escape velocity (score ≥ 9.0) or a confirmed breakout above VAH,
    # price will likely revert to POC rather than trend away from it.
    if sig.get("near_poc") and not sig.get("above_value_area"):
        if score < 9.0:
            poc_str = f"POC {float(poc):.2f}" if poc else ""
            return True, (
                f"Near POC {poc_str} — max-volume magnet, "
                "high reversion probability before any continuation move"
            )

    return False, ""


def _setup_quality_gate(sig: dict, setup: str, score: float) -> tuple[bool, str]:
    """Setup-type-specific entry quality checks beyond the composite signal score.

    Each setup type has structural requirements that the signal scorer already
    rewards/penalises, but a failed structural requirement is a hard veto here
    regardless of score — we won't enter a gap-and-go that hasn't gapped, etc.

    momentum      — EMA9 > EMA21 + price above VWAP (both required)
    gap_and_go    — gap ≥ config minimum + price cleared first-bar high or ORB-30
    vwap_reclaim  — price confirmed above VWAP; fresh cross preferred at lower scores
    mean_reversion — RSI in recovery zone (≤ 55) + anchored near POC or value area
    """
    above_vwap = bool(sig.get("above_vwap"))
    ema9  = float(sig.get("ema9",  0))
    ema21 = float(sig.get("ema21", 0))
    rsi   = float(sig.get("rsi",   50))

    if setup == "momentum":
        if not (ema9 > ema21 and above_vwap):
            missing = []
            if not ema9 > ema21:
                missing.append("EMA9>EMA21")
            if not above_vwap:
                missing.append("above VWAP")
            return True, f"Momentum requires {' + '.join(missing)} — not confirmed"

    elif setup == "gap_and_go":
        gap_pct   = float(sig.get("gap_pct", 0))
        above_fbh = bool(sig.get("above_first_bar_high"))
        above_orb = bool(sig.get("above_orb_30"))
        if gap_pct < config.GAP_AND_GO_MIN_PCT:
            return True, (
                f"Gap-and-go requires ≥{config.GAP_AND_GO_MIN_PCT}% gap — "
                f"only {gap_pct:.1f}% today"
            )
        if not above_fbh and not above_orb:
            return True, (
                "Gap-and-go: price has not cleared first-bar high or ORB-30 — "
                "no breakout confirmation, fakeout risk"
            )

    elif setup == "vwap_reclaim":
        if not above_vwap:
            return True, "VWAP reclaim: price still below VWAP — reclaim not confirmed"
        vwap_cross = bool(sig.get("vwap_cross_up"))
        # At lower scores require a fresh cross; strong scores can enter on continuation
        if not vwap_cross and score < 8.0:
            return True, (
                "VWAP reclaim: no fresh cross-up on this bar — "
                "chasing risk at this score level"
            )

    elif setup == "mean_reversion":
        if rsi > 55:
            return True, (
                f"Mean reversion: RSI {rsi:.0f} too high — "
                "not in reversal zone (need RSI ≤ 55 for credible reversion)"
            )
        # Must have a structural anchor to revert to
        if not sig.get("in_value_area") and not sig.get("near_poc"):
            return True, (
                "Mean reversion: not near POC or inside value area — "
                "no volume-based level to anchor the reversal"
            )

    return False, ""


def _enrichment_gate(item: dict, score: float) -> tuple[bool, str]:
    """Use enrichment data as a final quality filter — only vetoes, never promotes.

    Dark pool distribution signal → institutions are selling into retail buys → SKIP
    Strongly bearish options flow without a news catalyst → smart money short → SKIP

    These gates only trigger when the signal is unambiguous AND score is not
    extremely high (≥ 8.5), since a very strong price setup can override flow.
    """
    # Dark pool: distribution signal with low dark-pool participation pct
    dp = item.get("dark_pool", {})
    if dp and isinstance(dp, dict):
        dp_signal = str(dp.get("signal", "")).lower()
        dp_pct    = float(dp.get("dark_pool_pct", 50))
        if dp_signal == "distribution" and dp_pct < 30 and score < 8.5:
            return True, (
                f"Dark pool distribution ({dp_pct:.0f}% dark) — "
                "institutional selling pressure contradicts long entry"
            )

    # Options flow: elevated put-call ratio with unusual puts and no catalyst
    opts = item.get("options_flow", {})
    if opts and isinstance(opts, dict):
        put_call     = float(opts.get("put_call_ratio", 1.0))
        unusual_puts = bool(opts.get("unusual_puts"))
        catalyst_score = int(item.get("catalyst_score", 1 if item.get("has_catalyst") else 0))
        if put_call > 2.0 and unusual_puts and catalyst_score < 2 and score < 8.0:
            return True, (
                f"Options flow bearish (P/C {put_call:.1f}x, unusual puts, no catalyst) — "
                "smart money positioned short"
            )

    return False, ""
