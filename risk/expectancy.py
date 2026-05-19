import sqlite3
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import config
from core.database import log


class ExpectancyEngine:
    """Compute expectancy, cooling rules, Kelly sizing, and confidence drift."""

    _REVENGE_WINDOW_MINUTES = 90

    def __init__(self, db_path: str):
        """Initialize with the SQLite database used for historical trade queries.

        Args:
            db_path: SQLite path (used for confidence-drift queries).

        Returns:
            None.
        """
        self.db_path = db_path

    def compute_expectancy(self, decisions: list[dict], min_sample: int = 10) -> dict | None:
        """Compute overall expectancy from a list of closed trades.

        Args:
            decisions: List of decision dicts from the database.
            min_sample: Minimum closed trades required to return a result.

        Returns:
            Dict with win_rate, avg_win, avg_loss, expectancy, etc., or None
            if sample size is below min_sample.
        """
        closed = [d for d in decisions
                  if d.get("action") in ("SELL", "PARTIAL_SELL")
                  and d.get("pnl") is not None]
        if len(closed) < min_sample:
            return None

        wins       = [d["pnl"] for d in closed if d["pnl"] > 0]
        losses     = [abs(d["pnl"]) for d in closed if d["pnl"] < 0]
        breakevens = len([d for d in closed if d["pnl"] == 0])

        total     = len(closed)
        win_rate  = len(wins)   / total
        loss_rate = len(losses) / total
        avg_win   = sum(wins)   / len(wins)   if wins   else 0
        avg_loss  = sum(losses) / len(losses) if losses else 0
        expectancy = (win_rate * avg_win) - (loss_rate * avg_loss)

        # Slippage: sum from BUY records matched to these closed trades
        buys = [d for d in decisions if d.get("action") == "BUY"
                and d.get("slippage_dollars") is not None]
        total_slippage = sum(d["slippage_dollars"] for d in buys)
        avg_slippage   = total_slippage / len(buys) if buys else 0.0
        net_expectancy = expectancy - avg_slippage  # true edge after execution cost

        return {
            "total_trades":   total,
            "wins":           len(wins),
            "losses":         len(losses),
            "breakevens":     breakevens,
            "win_rate":       round(win_rate, 3),
            "loss_rate":      round(loss_rate, 3),
            "avg_win":        round(avg_win, 2),
            "avg_loss":       round(avg_loss, 2),
            "expectancy":     round(expectancy, 2),
            "total_slippage": round(total_slippage, 2),
            "avg_slippage":   round(avg_slippage, 4),
            "net_expectancy": round(net_expectancy, 2),
            "is_positive":    net_expectancy > 0,
        }

    def get_recent_consecutive_losses(self, decisions: list[dict]) -> int:
        """Count consecutive losing trades within the revenge-trade window.

        Walk backwards through SELL decisions and count how many in a row were losses,
        BUT only if they all occurred within the same 90-minute window.

        Rationale: 3 losses at 9:45, 12:30, and 2:15 are normal intraday variance.
        3 losses between 9:45 and 11:15 signal a genuinely broken strategy this session.
        """
        sells = [d for d in decisions
                 if d.get("action") in ("SELL", "PARTIAL_SELL")]

        streak = 0
        first_loss_ts: datetime | None = None

        for d in reversed(sells):
            pnl = d.get("pnl") or 0
            if pnl >= 0:
                break
            streak += 1

            ts_str = d.get("ts", "")
            try:
                _dt = datetime.fromisoformat(ts_str)
                ts  = _dt.replace(tzinfo=timezone.utc) if _dt.tzinfo is None else _dt.astimezone(timezone.utc)
            except Exception:
                ts = None

            if streak == 1 and ts:
                first_loss_ts = ts
            elif streak >= 2 and ts and first_loss_ts:
                window = abs((first_loss_ts - ts).total_seconds()) / 60
                if window > self._REVENGE_WINDOW_MINUTES:
                    # Losses too spread out  don't treat as a session breakdown
                    streak -= 1   # discount the earliest loss outside the window
                    break

        return streak

    def check_revenge_trade_guard(self, consecutive_losses: int,
                                   signal_confidence: int) -> tuple[bool, str]:
        """Gate entries when a streak of losses suggests a broken strategy.

        After 2 consecutive losses: require signal_confidence >= MIN + 1.
        After 3+ consecutive losses: require signal_confidence >= MIN + 2
        (stand-aside territory).

        Returns:
            Tuple of (allowed: bool, reason: str).
        """
        if consecutive_losses == 0:
            return True, "no consecutive losses"

        if consecutive_losses >= 3:
            required = config.MIN_SIGNAL_CONFIDENCE + 2
            if signal_confidence < required:
                return False, (f"Revenge-trade guard: {consecutive_losses} consecutive losses. "
                               f"Require confidence {required}/10 (have {signal_confidence}/10). "
                               f"Stand aside until edge fully confirmed. Rule 14.")
            return True, f"Consecutive losses={consecutive_losses}, confidence={signal_confidence} meets elevated bar"

        if consecutive_losses >= 2:
            required = config.MIN_SIGNAL_CONFIDENCE + 1
            if signal_confidence < required:
                return False, (f"Revenge-trade guard: {consecutive_losses} consecutive losses. "
                               f"Require confidence {required}/10 (have {signal_confidence}/10). Rule 14.")

        return True, f"Consecutive losses={consecutive_losses}, confidence OK"

    def expectancy_report(self, decisions: list[dict]) -> str:
        """Return a human-readable one-liner summarising expectancy for logging."""
        exp = self.compute_expectancy(decisions)
        if exp is None:
            return "expectancy: insufficient data"
        sign = "+" if exp["is_positive"] else ""
        return (f"expectancy: {sign}{exp['expectancy']:.2f}  "
                f"WR={exp['win_rate']:.0%}  "
                f"avgW=${exp['avg_win']:.1f}  avgL=${exp['avg_loss']:.1f}  "
                f"n={exp['total_trades']}")

    def compute_expectancy_by_setup(self, decisions: list[dict],
                                     min_sample: int = 3) -> dict[str, dict]:
        """Break down expectancy by setup_type.

        Used by the morning study and review_log to identify which setups are
        profitable and which to suppress.

        Args:
            decisions: List of decision dicts from the database.
            min_sample: Minimum trades per setup type to include in results.

        Returns:
            {setup_type: expectancy_dict}  only setups with >= min_sample trades.
        """
        buckets: dict[str, list[dict]] = defaultdict(list)

        for d in decisions:
            if d.get("action") not in ("SELL", "PARTIAL_SELL"):
                continue
            if d.get("pnl") is None:
                continue
            st = d.get("setup_type") or "unknown"
            buckets[st].append(d)

        result = {}
        for st, trades in buckets.items():
            exp = self.compute_expectancy(trades, min_sample=min_sample)
            if exp is not None:
                result[st] = exp
        return result

    def compute_dynamic_confidence_bar(self, decisions: list[dict]) -> int:
        """Dynamically raise the minimum signal confidence when recent form is poor.

        Looks at the last DYNAMIC_WINRATE_LOOKBACK closed trades.
        If win rate < DYNAMIC_WINRATE_THRESHOLD ? raise bar by 1 point.
        If win rate < DYNAMIC_WINRATE_THRESHOLD - 0.15 ? raise bar by 2 points
        (severe slump).

        Returns:
            The effective minimum confidence floor for this session.
        """
        closed = [
            d for d in decisions
            if d.get("action") in ("SELL", "PARTIAL_SELL") and d.get("pnl") is not None
        ][-config.DYNAMIC_WINRATE_LOOKBACK:]

        if len(closed) < 5:
            return config.MIN_SIGNAL_CONFIDENCE  # too few trades to adjust

        wins     = sum(1 for d in closed if d.get("pnl", 0) > 0)
        win_rate = wins / len(closed)

        if win_rate < (config.DYNAMIC_WINRATE_THRESHOLD - 0.15):
            bar = config.MIN_SIGNAL_CONFIDENCE + 2
            log.warning("Dynamic confidence bar RAISED to %d/10  severe slump WR=%.0f%%",
                        bar, win_rate * 100)
            return bar

        if win_rate < config.DYNAMIC_WINRATE_THRESHOLD:
            bar = config.MIN_SIGNAL_CONFIDENCE + 1
            log.warning("Dynamic confidence bar raised to %d/10  recent WR=%.0f%% below %.0f%% threshold",
                        bar, win_rate * 100, config.DYNAMIC_WINRATE_THRESHOLD * 100)
            return bar

        return config.MIN_SIGNAL_CONFIDENCE

    def get_cooling_symbols(self, decisions: list[dict]) -> dict[str, str]:
        """Return symbols in a cooling-off period due to persistent losing.

        A symbol cools off when its last SYMBOL_COOLING_LOOKBACK closed trades
        show a win rate below SYMBOL_COOLING_MIN_WIN_RATE. Prevents doubling
        down on a stock that is persistently losing.

        Returns:
            {symbol: reason} for each symbol currently in cooling-off.
        """
        closed = [d for d in decisions
                  if d.get("action") in ("SELL", "PARTIAL_SELL")
                  and d.get("pnl") is not None]

        # Collect up to LOOKBACK most-recent closed trades per symbol
        sym_trades: dict[str, list] = defaultdict(list)
        for d in reversed(closed):
            sym = d.get("symbol")
            if sym and len(sym_trades[sym]) < config.SYMBOL_COOLING_LOOKBACK:
                sym_trades[sym].append(d)

        cooling: dict[str, str] = {}
        for sym, trades in sym_trades.items():
            if len(trades) < config.SYMBOL_COOLING_LOOKBACK:
                continue  # not enough history yet
            wins = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
            wr   = wins / len(trades)
            if wr < config.SYMBOL_COOLING_MIN_WIN_RATE:
                cooling[sym] = (
                    f"{sym} cooling off: {wr:.0%} win rate on last "
                    f"{config.SYMBOL_COOLING_LOOKBACK} trades "
                    f"(< {config.SYMBOL_COOLING_MIN_WIN_RATE:.0%} threshold)  "
                    f"skip until form recovers"
                )
        return cooling

    def get_suppressed_setups(self, decisions: list[dict], min_sample: int = 5) -> dict[str, str]:
        """Mechanically suppress setup types with proven negative expectancy.

        Returns {setup_type: reason} for any setup that:
          - Has >= min_sample closed trades with P&L recorded
          - Has negative expectancy (avg loss > avg win  win rate)
          - Is not "unknown" (untagged trades shouldn't suppress valid setups)

        The caller blocks new BUYs of that setup type for the rest of the session.
        This is Rule 19 enforced in code, not just advisory text to Claude.
        """
        by_setup = self.compute_expectancy_by_setup(decisions, min_sample=min_sample)
        suppressed: dict[str, str] = {}
        for st, exp in by_setup.items():
            if st in ("unknown", "", None):
                continue
            if not exp["is_positive"]:
                suppressed[st] = (
                    f"Setup '{st}' suppressed (Rule 19): E=${exp['expectancy']:.2f} "
                    f"WR={exp['win_rate']:.0%} avgW=${exp['avg_win']:.0f} "
                    f"avgL=${exp['avg_loss']:.0f} over {exp['total_trades']} trades"
                )
        return suppressed

    def compute_kelly_factor(self, decisions: list[dict]) -> float:
        """Compute the half-Kelly position-size multiplier from historical performance.

        Full Kelly fraction: f = WR - (LR / realized_RR).
        We apply 0.5 (half-Kelly) for safety and normalize around a reference Kelly
        of 0.20 so that a system with "normal" edge maps to factor 1.0.

        Returns:
            Multiplier in [0.25, 1.5]:
              > 1.0   strong historical edge ? size up relative to base
              = 1.0   neutral / insufficient data
              < 1.0   weak or negative edge ? size down as early warning
            Requires at least 10 closed trades with P&L recorded.
        """
        closed = [d for d in decisions
                  if d.get("action") in ("SELL", "PARTIAL_SELL")
                  and d.get("pnl") is not None]
        if len(closed) < 10:
            return 1.0

        wins   = [d["pnl"] for d in closed if d["pnl"] > 0]
        losses = [abs(d["pnl"]) for d in closed if d["pnl"] < 0]

        if not wins or not losses:
            return 1.0

        win_rate  = len(wins) / len(closed)
        loss_rate = 1.0 - win_rate
        avg_win   = sum(wins)   / len(wins)
        avg_loss  = sum(losses) / len(losses)
        rr        = avg_win / avg_loss if avg_loss > 0 else 1.0

        full_kelly = win_rate - (loss_rate / rr)

        if full_kelly <= 0:
            return 0.5  # negative edge ? cut size as a warning signal

        # Normalize: a system with Kelly=0.20 is "baseline normal" ? factor 1.0
        # factor = (half_kelly) / (reference_kelly * 0.5)
        REFERENCE_KELLY = 0.20
        factor = (full_kelly * 0.5) / (REFERENCE_KELLY * 0.5)

        return round(max(0.25, min(factor, 1.5)), 3)

    def get_confidence_drift(self) -> dict | None:
        """Compare 7-day rolling avg confidence to the 90-day baseline.

        Returns a drift report dict if deviation > CONFIDENCE_DRIFT_THRESHOLD,
        otherwise None (healthy). Requires at least 10 BUY decisions with
        confidence recorded.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        rows = conn.execute(
            """SELECT confidence, ts FROM decisions
               WHERE action = 'BUY' AND confidence IS NOT NULL
               ORDER BY ts DESC LIMIT 500"""
        ).fetchall()
        conn.close()

        if len(rows) < 10:
            return None

        now        = datetime.now(timezone.utc)
        cutoff_7d  = (now - timedelta(days=7)).isoformat()
        cutoff_90d = (now - timedelta(days=90)).isoformat()

        recent   = [int(r[0]) for r in rows if r[1] >= cutoff_7d]
        baseline = [int(r[0]) for r in rows if r[1] >= cutoff_90d]

        if len(recent) < 5 or len(baseline) < 10:
            return None

        recent_avg   = sum(recent)   / len(recent)
        baseline_avg = sum(baseline) / len(baseline)
        drift        = recent_avg - baseline_avg

        if abs(drift) < config.CONFIDENCE_DRIFT_THRESHOLD:
            return None

        return {
            "recent_avg":   round(recent_avg,   2),
            "baseline_avg": round(baseline_avg, 2),
            "drift":        round(drift,         2),
            "recent_n":     len(recent),
            "baseline_n":   len(baseline),
            "direction":    "HIGH (possibly overconfident)" if drift > 0 else "LOW (possibly too cautious)",
        }

    def setup_expectancy_report(self, decisions: list[dict]) -> str:
        """Return a multi-line human-readable setup breakdown for logging and email."""
        by_setup = self.compute_expectancy_by_setup(decisions)
        if not by_setup:
            return "setup expectancy: insufficient data per setup type"

        lines = ["Setup-type expectancy breakdown:"]
        for st, exp in sorted(by_setup.items(),
                               key=lambda x: x[1]["expectancy"], reverse=True):
            sign   = "+" if exp["is_positive"] else ""
            flag   = " ?" if exp["is_positive"] else " ? SUPPRESS"
            lines.append(f"  {st[:28]:28s} | E={sign}{exp['expectancy']:.2f} "
                         f"WR={exp['win_rate']:.0%} "
                         f"avgW=${exp['avg_win']:.0f} avgL=${exp['avg_loss']:.0f} "
                         f"n={exp['total_trades']}{flag}")
        return "\n".join(lines)
