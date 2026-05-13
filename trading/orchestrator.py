"""
Options trading orchestrator: composes all mixins into one runnable object
and manages session lifecycle — daily reset, EOD close, and summary writing.

Mixin composition (MRO order):
  TradingOrchestrator
    OptionsPositionsMixin  — 2-min position monitor
    OptionsExecutorMixin   — ENTER decision → order submission
    OptionsTradeCycleMixin — 5-min scan, IV data, decisions

All shared state lives here; mixins read and mutate it via `self`.
"""

from __future__ import annotations

import threading
from datetime import datetime

import config
from core.database import log
from trading.positions import OptionsPositionsMixin
from trading.executor  import OptionsExecutorMixin
from trading.trade_cycle import OptionsTradeCycleMixin


class TradingOrchestrator(OptionsPositionsMixin, OptionsExecutorMixin, OptionsTradeCycleMixin):
    """
    Top-level orchestrator: wires components together and owns session state.

    All public methods are called directly by the APScheduler jobs in main.py.
    """

    def __init__(
        self,
        broker,
        iv_analyzer,
        options_flow,
        options_risk,
        greeks_engine,
        strategy_selector,
        algo_engine,
        market_guard,
        market_analyst,
        screener,
        dark_pool,
        pre_market,
        yield_curve,
        edgar,
        notifier,
        database,
        dynamic_watchlist,
        signal_scorer,
    ):
        """
        Inject all external dependencies.

        Args:
            broker:            AlpacaBroker (also OptionsOrdersMixin).
            iv_analyzer:       IVAnalyzer for IV rank, VRP, and regime.
            options_flow:      OptionsFlowClient for chain data and flow signals.
            options_risk:      OptionsRiskManager instance.
            greeks_engine:     GreeksEngine for strike selection and P&L.
            strategy_selector: OptionsStrategySelector.
            algo_engine:       OptionsDecisionEngine.
            market_guard:      MarketGuard for circuit breakers and VIX regime.
            market_analyst:    Morning study and plan persistence.
            screener:          Universe builder.
            dark_pool:         Dark pool signal client.
            pre_market:        Pre-market data client.
            yield_curve:       Yield curve macro client.
            edgar:             EDGAR 8-K gate client.
            notifier:          Email alert notifier.
            database:          SQLite database handle.
            dynamic_watchlist: Carry-forward watchlist store.
            signal_scorer:     SignalScorer for watchlist scoring.
        """
        self.broker            = broker
        self.iv_analyzer       = iv_analyzer
        self.options_flow      = options_flow
        self.options_risk      = options_risk
        self.greeks_engine     = greeks_engine
        self.strategy_selector = strategy_selector
        self.algo_engine       = algo_engine
        self.market_guard      = market_guard
        self.market_analyst    = market_analyst
        self.screener          = screener
        self.dark_pool         = dark_pool
        self.pre_market        = pre_market
        self.yield_curve       = yield_curve
        self.edgar             = edgar
        self.notifier          = notifier
        self.database          = database
        self.dynamic_watchlist = dynamic_watchlist
        self.signal_scorer     = signal_scorer

        # ── Per-session state ─────────────────────────────────────────────────
        self._daily_pnl:        float       = 0.0
        self._session_date:     str         = ""
        self._daily_plan:       dict | None = None
        self._study_complete:   bool        = False
        self._dry_run:          bool        = False
        self._eod_done:         bool        = False
        self._force_run:        bool        = False
        self._current_vix:      float       = 20.0
        self._consecutive_losses: int       = 0
        self._last_scan_ts:     datetime | None = None

        # ── Locks ─────────────────────────────────────────────────────────────
        self._state_lock  = threading.Lock()
        self._broker_lock = threading.Lock()
        self._scan_lock   = threading.Lock()
        self._scan_generation = 0

        self._ET = config.ET
        self._SCAN_TIMEOUT_SECONDS = 480

    # ── Control flags ─────────────────────────────────────────────────────────

    def set_force_run(self, flag: bool) -> None:
        """Bypass market-hours and study gates (testing only)."""
        self._force_run = flag
        if flag:
            log.info("=== FORCE MODE: market-hours gates bypassed ===")

    def set_dry_run(self, flag: bool) -> None:
        """Log decisions but submit no real orders."""
        self._dry_run = flag
        if flag:
            log.info("=== DRY-RUN MODE: no orders will be placed ===")

    # ── Daily reset ───────────────────────────────────────────────────────────

    def reset_daily_state(self) -> None:
        """
        Reset all per-session counters and caches at the start of a new trading day.

        Called automatically when the session date changes.
        """
        self._daily_pnl          = 0.0
        self._session_date       = datetime.now(self._ET).date().isoformat()
        self._daily_plan         = None
        self._study_complete     = False
        self._eod_done           = False
        self._consecutive_losses = 0
        self._last_scan_ts       = None
        self.market_guard.reset_circuit_breaker()
        self.market_guard.reset_earnings_cache()
        self.market_guard.reset_intraday_regime()
        log.info("=== Daily state reset for %s ===", self._session_date)

    # ── Time helpers ──────────────────────────────────────────────────────────

    def is_in_study_window(self, hour: int, minute: int) -> bool:
        """Return True when the clock is inside the pre-market study window."""
        cur   = hour * 60 + minute
        start = config.STUDY_START_HOUR * 60 + config.STUDY_START_MIN
        end   = config.STUDY_END_HOUR   * 60 + config.STUDY_END_MIN
        return start <= cur < end

    def is_high_volume_window(self, hour: int, minute: int) -> bool:
        """Return True when the clock is in a configured high-volume window."""
        cur = hour * 60 + minute
        for sh, sm, eh, em in config.HIGH_VOLUME_WINDOWS:
            if (sh * 60 + sm) <= cur <= (eh * 60 + em):
                return True
        return False

    # ── EOD ───────────────────────────────────────────────────────────────────

    def write_daily_summary(self) -> None:
        """
        Persist a daily summary row and send the end-of-day email.

        Aggregates closed options decisions from today's database records.
        """
        today     = datetime.now(self._ET).date().isoformat()
        decisions = self.database.get_today_options_decisions()

        enters = sum(1 for d in decisions if d.get("action") == "ENTER")
        closes = sum(1 for d in decisions if d.get("action") == "CLOSE")
        skips  = sum(1 for d in decisions if d.get("action") == "SKIP")

        closed_positions = [
            p for p in self.database.get_open_options_positions()
            if p.get("status") == "closed"
               and (p.get("close_ts") or "").startswith(today)
        ]
        wins   = sum(1 for p in closed_positions if (p.get("realized_pnl") or 0) > 0)
        losses = sum(1 for p in closed_positions if (p.get("realized_pnl") or 0) < 0)
        gross  = sum((p.get("realized_pnl") or 0) for p in closed_positions)

        self.database.upsert_daily_summary(
            today, enters + closes, wins, losses, gross, gross,
            notes=f"enters={enters} closes={closes} skips={skips}",
        )
        log.info("=== Daily summary %s | enters=%d closes=%d pnl=%.2f W=%d L=%d ===",
                 today, enters, closes, gross, wins, losses)
        self.notifier.send_daily_summary()

    # ── Scan thread wrapper ───────────────────────────────────────────────────

    def run_scan_and_trade(self) -> None:
        """
        Run _scan_body in a daemon thread with wall-clock timeout protection.

        If the previous scan is still running, this tick is skipped (non-blocking
        acquisition on the scan lock).

        Returns:
            None.
        """
        if not self._scan_lock.acquire(blocking=False):
            log.warning("Previous scan still running — skipping this tick")
            return
        try:
            my_gen = self._scan_generation
            t = threading.Thread(
                target=self._scan_body, args=(my_gen,),
                daemon=True, name="options-scan-body",
            )
            t.start()
            t.join(timeout=self._SCAN_TIMEOUT_SECONDS)
            if t.is_alive():
                log.error("SCAN TIMEOUT after %ds — scan thread hung", self._SCAN_TIMEOUT_SECONDS)
                t.join(timeout=120)
                if t.is_alive():
                    self._scan_generation += 1
                    log.error("Scan thread still alive — releasing lock, gen %d abandoned", my_gen)
        finally:
            self._scan_lock.release()
