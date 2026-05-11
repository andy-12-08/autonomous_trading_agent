"""
Algorithmic market analyst — replaces the LLM morning study.

Determines the daily trading plan (posture, bias, sectors) from market data alone:
  1. Economic calendar  → stand_aside on FOMC / major macro events
  2. VIX proxy (UVXY)   → conservative when fear is elevated
  3. SPY trend           → market_bias (bullish / bearish / neutral)
  4. Market breadth      → downgrade posture when breadth is weak
  5. Sector ETF returns  → sectors_to_favour / sectors_to_avoid
"""

import json
import sqlite3
from datetime import datetime

import config
from agents.study_data import StudyDataMixin
from core.database import log


_SECTOR_ETF_MAP = {
    "XLK": "tech",
    "XLF": "finance",
    "XLE": "energy",
    "XLV": "healthcare",
    "XLY": "consumer",
    "XLI": "industrial",
}


class AlgoMarketAnalyst(StudyDataMixin):
    """Algorithmic replacement for MarketAnalyst — no LLM required.

    Keeps the same interface as MarketAnalyst so the orchestrator needs no changes:
      load_todays_plan()       — SQLite cache lookup (identical implementation)
      run_morning_study(acct)  — algorithmic plan generation
    """

    def __init__(self, broker, indicators, pre_market, yield_curve,
                 short_interest, dynamic_watchlist):
        """Wire broker, indicators, and market-data clients.

        Args:
            broker: AlpacaBroker for prices, bars, and snapshot data.
            indicators: IndicatorEngine for technical context.
            pre_market: Pre-market range helper for gap analysis.
            yield_curve: Yield curve data client for macro context.
            short_interest: Short interest data client (unused in algo study; kept for interface parity).
            dynamic_watchlist: DynamicWatchlist for carryover symbol persistence.
        """
        self.broker            = broker
        self.indicators        = indicators
        self.pre_market        = pre_market
        self.yc                = yield_curve
        self.si                = short_interest
        self.dynamic_watchlist = dynamic_watchlist

    # ── Persistence helpers (identical to MarketAnalyst) ────────────────────────

    def _save_daily_plan(self, plan: dict) -> None:
        """Persist the plan dict to the daily_plans table keyed by date.

        Args:
            plan: Daily plan dict; uses its date key or today when missing.

        Returns:
            None.
        """
        conn = sqlite3.connect(config.DB_PATH, timeout=10)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_plans (
                date  TEXT PRIMARY KEY,
                plan  TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT OR REPLACE INTO daily_plans (date, plan) VALUES (?,?)",
            (plan.get("date", datetime.now(config.ET).date().isoformat()), json.dumps(plan)),
        )
        conn.commit()
        conn.close()

    def load_todays_plan(self) -> dict | None:
        """Load today's plan from SQLite if the morning study already ran.

        Returns:
            Plan dict, or None if no entry exists for today.
        """
        conn = sqlite3.connect(config.DB_PATH, timeout=10)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_plans (
                date TEXT PRIMARY KEY,
                plan TEXT NOT NULL
            )
        """)
        row = conn.execute(
            "SELECT plan FROM daily_plans WHERE date=?",
            (datetime.now(config.ET).date().isoformat(),),
        ).fetchone()
        conn.commit()
        conn.close()
        return json.loads(row[0]) if row else None

    # ── Core study ───────────────────────────────────────────────────────────────

    def run_morning_study(self, account: dict) -> dict:
        """Build the daily trading plan from market data alone, no LLM.

        Args:
            account: settled_cash, total_equity, deployed_today, etc.

        Returns:
            Daily trading plan dict with posture, bias, sectors, and warnings.
        """
        log.info("=== ALGORITHMIC MORNING STUDY START ===")

        today      = datetime.now(config.ET).date().isoformat()
        econ       = self._get_economic_calendar()
        market_ctx = self._get_market_context()

        gappers, breadth = self._get_gap_and_breadth(config.WATCHLIST)
        macro_flag       = econ.get("macro_flag", "none")

        # ── 1. Start from a neutral posture ──────────────────────────────────────
        risk_posture   = "normal"
        special_warnings: list[str] = []

        # ── 2. Hard macro gates ───────────────────────────────────────────────────
        if econ.get("is_fomc_day"):
            risk_posture = "stand_aside"
            special_warnings.append("FOMC DAY — stand_aside enforced by economic calendar guard")
            macro_flag   = "stand_aside"
        elif macro_flag == "stand_aside":
            risk_posture = "stand_aside"
            high_events  = econ.get("high_impact", [])
            event_titles = ", ".join(e.get("title", "") for e in high_events[:3])
            special_warnings.append(f"High-impact macro event today: {event_titles}")
        elif macro_flag == "caution":
            risk_posture = "conservative"
            special_warnings.append("Macro caution: significant economic release — be conservative")

        # ── 3. VIX proxy: UVXY day change ────────────────────────────────────────
        # UVXY rises when volatility spikes. >5% intraday = fear elevated;
        # >10% = fear spike; circuit breaker is the hard stop, this is sizing signal.
        uvxy_ctx    = market_ctx.get("UVXY", {})
        uvxy_change = float(uvxy_ctx.get("day_change_pct", 0))
        if uvxy_change > 10 and risk_posture == "normal":
            risk_posture = "conservative"
            special_warnings.append(f"UVXY +{uvxy_change:.1f}% — fear spike, reducing aggression")
        elif uvxy_change > 5 and risk_posture == "normal":
            special_warnings.append(f"UVXY +{uvxy_change:.1f}% — elevated volatility, stay disciplined")

        # ── 4. SPY trend → market bias ────────────────────────────────────────────
        spy_ctx       = market_ctx.get("SPY", {})
        spy_change    = float(spy_ctx.get("day_change_pct", 0))
        spy_above_vwap = bool(spy_ctx.get("above_vwap", True))
        spy_ema_bull  = spy_ctx.get("ema_trend", "bullish") == "bullish"
        spy_rsi       = float(spy_ctx.get("rsi", 50))

        if spy_change > 0.5 and spy_above_vwap and spy_ema_bull:
            market_bias = "bullish"
        elif spy_change < -0.5 and not spy_above_vwap:
            market_bias = "bearish"
            if risk_posture == "normal":
                risk_posture = "conservative"
                special_warnings.append(
                    f"SPY {spy_change:+.1f}% below VWAP — bearish broad market, conservative posture")
        else:
            market_bias = "neutral"

        # ── 5. Market breadth confirmation / downgrade ────────────────────────────
        breadth_cond = breadth.get("breadth_condition", "NEUTRAL")
        if breadth_cond == "WEAK" and risk_posture == "normal":
            risk_posture = "conservative"
            special_warnings.append(f"Market breadth WEAK — conservative posture")
        elif breadth_cond == "STRONG" and market_bias == "bullish" and risk_posture == "conservative":
            # Don't upgrade past conservative from breadth alone — macro gates take priority
            pass

        # ── 6. Sector classification from ETF day-change ──────────────────────────
        sectors_to_favour: list[str] = []
        sectors_to_avoid:  list[str] = []
        for etf, sector in _SECTOR_ETF_MAP.items():
            ctx = market_ctx.get(etf, {})
            ch  = float(ctx.get("day_change_pct", 0))
            if ch > 0.5:
                sectors_to_favour.append(sector)
            elif ch < -0.5:
                sectors_to_avoid.append(sector)

        # ── 7. Setup preferences based on bias ────────────────────────────────────
        if market_bias == "bullish":
            setups_to_use   = ["momentum", "gap_and_go", "vwap_reclaim"]
            setups_to_avoid = ["mean_reversion"]
        elif market_bias == "bearish":
            setups_to_use   = ["vwap_reclaim", "mean_reversion"]
            setups_to_avoid = ["gap_and_go", "momentum"]
        else:
            setups_to_use   = ["vwap_reclaim", "momentum"]
            setups_to_avoid = []

        # ── 8. Top candidates from gap scan ───────────────────────────────────────
        top_candidates = [
            {"symbol": g["symbol"], "gap_pct": g.get("change_pct", 0)}
            for g in gappers[:15]
            if g.get("change_pct", 0) >= config.GAP_AND_GO_MIN_PCT
        ]

        # ── 9. Assemble plan ──────────────────────────────────────────────────────
        plan = {
            "date":                        today,
            "market_bias":                 market_bias,
            "risk_posture":                risk_posture,
            "macro_event_flag":            macro_flag,
            "market_summary":              (
                f"SPY {spy_change:+.1f}% RSI={spy_rsi:.0f} {'above' if spy_above_vwap else 'below'} VWAP | "
                f"UVXY {uvxy_change:+.1f}% | Breadth: {breadth_cond}"
            ),
            "vix_proxy_note":              f"UVXY {uvxy_change:+.1f}% intraday",
            "breadth_summary":             breadth.get("summary", breadth_cond),
            "daily_profit_target_dollars": 100,
            "daily_max_loss_dollars":      config.DAILY_DRAWDOWN_LIMIT,
            "sectors_to_favour":           sectors_to_favour,
            "sectors_to_avoid":            sectors_to_avoid,
            "setups_to_use":               setups_to_use,
            "setups_to_avoid":             setups_to_avoid,
            "top_candidates":              top_candidates,
            "history_lessons":             [],
            "special_warnings":            special_warnings,
            "breadth_condition":           breadth_cond,
        }

        self._save_daily_plan(plan)

        log.info("=== ALGORITHMIC MORNING STUDY COMPLETE ===")
        log.info(
            "Bias=%-8s  Posture=%-12s  Candidates=%d  Breadth=%s  Macro=%s",
            plan["market_bias"], plan["risk_posture"],
            len(top_candidates), breadth_cond, macro_flag,
        )
        if sectors_to_favour:
            log.info("Sectors to favour: %s", sectors_to_favour)
        if sectors_to_avoid:
            log.info("Sectors to avoid:  %s", sectors_to_avoid)
        for w in special_warnings:
            log.warning("  WARNING: %s", w)
        if gappers:
            log.info("Top gappers: %s",
                     ", ".join(f"{g['symbol']} {g['change_pct']:+.1f}%" for g in gappers[:5]))

        return plan
