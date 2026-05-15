from datetime import datetime
import threading

import config
from core.database import log
from trading.scanner import ScannerMixin
from trading.positions import PositionsMixin
from trading.executor import ExecutorMixin
from trading.trade_cycle import TradeCycleMixin


class TradingOrchestrator(ScannerMixin, PositionsMixin, ExecutorMixin, TradeCycleMixin):
    """Compose mixins for scanning, position management, execution, and the main scan cycle."""

    def __init__(
        self,
        broker,
        indicators,
        risk_manager,
        bucket_manager,
        gfv_tracker,
        signal_scorer,
        expectancy_engine,
        options_flow,
        insider_flow,
        dark_pool,
        pre_market,
        yield_curve,
        short_interest,
        edgar,
        algo_engine,
        market_analyst,
        market_guard,
        notifier,
        screener,
        dynamic_watchlist,
        float_cache,
        session_overrides,
        database,
    ):
        """Wire broker, analytics, risk, data feeds, algo engines, persistence, and session state.

        Args:
            broker: AlpacaBroker for orders and market data.
            indicators: IndicatorEngine for bar-derived signals.
            risk_manager: RiskManager for sizing and approval rules.
            bucket_manager: BucketManager for sector exposure.
            gfv_tracker: GFVTracker for cash-account settlement tagging.
            signal_scorer: SignalScorer for watchlist filtering.
            expectancy_engine: ExpectancyEngine for Kelly and cooling rules.
            options_flow: Client for optional flow data.
            insider_flow: Client for insider transaction data.
            dark_pool: Client for dark pool summaries.
            pre_market: Pre-market level helper.
            yield_curve: Yield curve client for macro sizing.
            short_interest: Short interest client.
            edgar: EDGAR filing gate client.
            algo_engine: Algorithmic decision engine.
            market_analyst: Morning study and plan persistence.
            market_guard: Circuit breaker and regime helpers.
            notifier: Email notifier.
            screener: Universe builder.
            dynamic_watchlist: Carry-forward watchlist store.
            float_cache: FloatCache for per-symbol float share lookups.
            session_overrides: Study-driven threshold overrides.
            database: Database handle for SQLite.
        """
        self.broker            = broker
        self.indicators        = indicators
        self.risk_manager      = risk_manager
        self.bucket_manager    = bucket_manager
        self.gfv_tracker       = gfv_tracker
        self.signal_scorer     = signal_scorer
        self.expectancy_engine = expectancy_engine
        self.options_flow      = options_flow
        self.insider_flow      = insider_flow
        self.dark_pool         = dark_pool
        self.pre_market        = pre_market
        self.yield_curve       = yield_curve
        self.short_interest    = short_interest
        self.edgar             = edgar
        self.algo_engine       = algo_engine
        self.market_analyst    = market_analyst
        self.market_guard      = market_guard
        self.notifier          = notifier
        self.screener          = screener
        self.dynamic_watchlist = dynamic_watchlist
        self.float_cache       = float_cache
        self.session_overrides = session_overrides
        self.database          = database

        self._deployed_today:       float          = 0.0
        self._daily_pnl:            float          = 0.0
        self._trades_today:         int            = 0
        self._traded_buckets_today: set            = set()
        self._session_date:         str            = ""
        self._daily_plan:           dict | None    = None
        self._study_complete:       bool           = False
        self._dry_run:              bool           = False
        self._last_full_scan_ts:    datetime | None = None
        self._key_levels_cache:     dict           = {}
        self._eod_done:             bool           = False
        self._daily_pre_passed:     set            = set()
        self._force_run:            bool           = False
        self._spy_trend_ok:         bool           = True  # updated each scan; False = SPY trending down

        self._state_lock  = threading.Lock()
        self._broker_lock = threading.Lock()
        self._scan_lock       = threading.Lock()
        self._scan_generation = 0  # incremented when a scan thread is abandoned

        self._ET = config.ET
        self._SCAN_TIMEOUT_SECONDS = 480

    def set_force_run(self, flag: bool):
        """Bypass market-hours and study gates so the pipeline can run at any time.

        Intended for testing; pair with set_dry_run(True) to avoid live orders.

        Args:
            flag: When True, force mode is enabled.

        Returns:
            None.
        """
        self._force_run = flag
        if flag:
            log.info("=== FORCE MODE: market-hours gates bypassed — pipeline will run immediately ===")

    def set_dry_run(self, flag: bool):
        """Enable or disable dry-run mode (no broker orders).

        Args:
            flag: When True, decisions are logged only.

        Returns:
            None.
        """
        self._dry_run = flag
        if flag:
            log.info("=== DRY-RUN MODE: market data and algo decisions will run, but NO real orders will be placed ===")

    def reset_daily_state(self):
        """Reset per-day counters, cached plan, and market-guard session state.

        On a same-day restart the counters are restored from today's DB decisions so
        that P&L, trade count, and deployed capital survive bot restarts.

        Returns:
            None.
        """
        self._deployed_today       = 0.0
        self._daily_pnl            = 0.0
        self._trades_today         = 0
        self._traded_buckets_today = set()
        self._session_date         = datetime.now(self._ET).date().isoformat()
        self._daily_plan           = None
        self._study_complete       = False
        self._last_full_scan_ts    = None
        self._eod_done             = False
        self._daily_pre_passed     = set()
        self.market_guard.reset_circuit_breaker()
        self.market_guard.reset_earnings_cache()
        self.market_guard.reset_intraday_regime()
        self.session_overrides.reset()
        log.info("=== Daily state reset for %s ===", self._session_date)
        self._restore_daily_state_from_db()

    def _restore_daily_state_from_db(self) -> None:
        """Rebuild today's P&L, trade count, and deployed capital from the decisions DB.

        Called after reset_daily_state so that same-day restarts pick up where they
        left off instead of starting from zero.

        Returns:
            None.
        """
        today = self._session_date
        try:
            decisions = self.database.get_recent_decisions(500)
        except Exception as exc:
            log.warning("Could not restore daily state from DB: %s", exc)
            return

        today_dec = [d for d in decisions if (d.get("ts") or "").startswith(today)]
        if not today_dec:
            return

        trades   = sum(1 for d in today_dec if d.get("action") == "BUY")
        realized = sum((d.get("pnl") or 0) for d in today_dec
                       if d.get("action") in ("SELL", "PARTIAL_SELL"))
        deployed = sum(
            (d.get("price") or 0) * (d.get("qty") or 0)
            for d in today_dec if d.get("action") == "BUY"
        )

        self._trades_today   = trades
        self._daily_pnl      = realized
        self._deployed_today = deployed
        log.info("Restored daily state from DB: trades=%d pnl=%.2f deployed=%.0f",
                 trades, realized, deployed)

    def is_in_study_window(self, hour: int, minute: int) -> bool:
        """Return True when the clock lies inside the morning study window (ET).

        Args:
            hour: Hour of day in Eastern Time, 0 to 23.
            minute: Minute of the hour, 0 to 59.

        Returns:
            True inside the configured study window, otherwise False.
        """
        cur   = hour * 60 + minute
        start = config.STUDY_START_HOUR * 60 + config.STUDY_START_MIN
        end   = config.STUDY_END_HOUR   * 60 + config.STUDY_END_MIN
        return start <= cur < end

    def is_high_volume_window(self, hour: int, minute: int) -> bool:
        """Return True when the clock lies inside a configured high-volume window (ET).

        Args:
            hour: Hour of day in Eastern Time.
            minute: Minute of the hour.

        Returns:
            True when the time falls in HIGH_VOLUME_WINDOWS, otherwise False.
        """
        cur = hour * 60 + minute
        for sh, sm, eh, em in config.HIGH_VOLUME_WINDOWS:
            if (sh * 60 + sm) <= cur <= (eh * 60 + em):
                return True
        return False

    def eod_close_all(self):
        """Close every open position at end of day; dry-run logs only.

        Returns:
            None.
        """
        if self._dry_run:
            positions = self.broker.get_positions()
            log.info("[DRY-RUN] EOD: would close %d position(s) — no real orders placed", len(positions))
            for sym in positions:
                log.info("  [DRY-RUN] Would close %s", sym)
            return
        log.info("EOD: closing all open positions")
        positions  = self.broker.get_positions()
        any_failed = False
        for symbol, pos in positions.items():
            gfv_safe, reason = self.gfv_tracker.gfv_safe_to_sell(symbol)
            if not gfv_safe:
                log.warning("EOD: GFV block on %s — %s. Closing anyway (EOD mandatory).", symbol, reason)
            pnl           = float(getattr(pos, "unrealized_pl",  0) or 0)
            current_price = float(getattr(pos, "current_price",  0) or 0)
            qty           = float(getattr(pos, "qty",            0) or 0)
            if not self.broker.close_position(symbol):
                log.error("EOD: broker rejected close for %s — position left open, skipping DB cleanup",
                          symbol)
                any_failed = True
                continue
            self.database.remove_position(symbol)
            self.gfv_tracker.remove_buy(symbol)
            with self._state_lock:
                self._daily_pnl += pnl
            self.database.record_decision(symbol, "SELL", price=current_price, qty=qty,
                            pnl=pnl, reasoning="EOD forced close — no overnight holds")
            self.database.update_outcome(symbol, "win" if pnl > 0 else "loss" if pnl < 0 else "breakeven", pnl)
            log.info("EOD closed %s | qty=%.0f price=%.2f pnl=%+.2f", symbol, qty, current_price, pnl)
        if any_failed:
            log.warning("EOD: one or more positions failed to close — skipping cancel_all_orders "
                        "to preserve bracket stops on still-open positions")
        else:
            self.broker.cancel_all_orders()

    def write_daily_summary(self):
        """Write daily_summary, persist the dynamic watchlist, and queue summary email.

        Returns:
            None.
        """
        today     = datetime.now(self._ET).date().isoformat()
        all_dec   = self.database.get_recent_decisions(200)
        today_dec = [d for d in all_dec if d["ts"].startswith(today)]
        trades    = sum(1 for d in today_dec if d["action"] in ("BUY", "SELL", "PARTIAL_SELL"))
        wins      = sum(1 for d in today_dec if (d.get("pnl") or 0) > 0)
        losses    = sum(1 for d in today_dec if (d.get("pnl") or 0) < 0)
        gross     = sum((d.get("pnl") or 0) for d in today_dec)

        exp_str  = self.expectancy_engine.expectancy_report(all_dec)
        exp_data = self.expectancy_engine.compute_expectancy(all_dec)

        setup_exp_str = self.expectancy_engine.setup_expectancy_report(all_dec)

        plan_note = ""
        if self._daily_plan:
            plan_note = (f"bias={self._daily_plan.get('market_bias')} "
                         f"target=${self._daily_plan.get('daily_profit_target_dollars')}")
        if exp_data:
            plan_note += f" | {exp_str}"
            if not exp_data["is_positive"]:
                log.warning("⚠ NEGATIVE EXPECTANCY: %.2f — review strategy before next session",
                            exp_data["expectancy"])

        self.database.upsert_daily_summary(today, trades, wins, losses, gross, gross, notes=plan_note)
        log.info("=== Daily summary %s | trades=%d W=%d L=%d pnl=%.2f ===",
                 today, trades, wins, losses, gross)
        log.info("    %s", exp_str)
        log.info("    %s", setup_exp_str)

        self.dynamic_watchlist.save(list(self._daily_pre_passed))

        self.notifier.send_daily_summary()

    def run_scan_and_trade(self):
        """Run _scan_body in a daemon thread with a wall-clock join timeout.

        The scan lock is held for the entire lifetime of the worker thread — if the
        thread hangs past the timeout, this method blocks until it finishes before
        releasing the lock. This prevents a second scan from starting while the first
        is still mutating shared state.

        Returns:
            None.
        """
        if not self._scan_lock.acquire(blocking=False):
            log.warning("Previous scan still running — skipping this 5-min tick")
            return
        try:
            my_gen = self._scan_generation
            t = threading.Thread(
                target=self._scan_body, args=(my_gen,), daemon=True, name="scan-body"
            )
            t.start()
            t.join(timeout=self._SCAN_TIMEOUT_SECONDS)
            if t.is_alive():
                log.error(
                    "SCAN TIMEOUT after %ds — scan thread is stuck (hung API call?). "
                    "Waiting up to 120s for clean exit before releasing lock.",
                    self._SCAN_TIMEOUT_SECONDS,
                )
                t.join(timeout=120)
                if t.is_alive():
                    self._scan_generation += 1  # invalidate the abandoned thread
                    log.error(
                        "Scan thread still alive after grace period — releasing lock. "
                        "Abandoned thread (gen %d) will not execute decisions.",
                        my_gen,
                    )
        finally:
            self._scan_lock.release()

