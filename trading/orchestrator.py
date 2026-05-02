from datetime import date, datetime
import threading

import config
from core.database import log
from trading.scanner import ScannerMixin
from trading.positions import PositionsMixin
from trading.executor import ExecutorMixin
from trading.trade_cycle import TradeCycleMixin


class TradingOrchestrator(ScannerMixin, PositionsMixin, ExecutorMixin, TradeCycleMixin):
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
        trading_agent,
        market_analyst,
        market_guard,
        notifier,
        screener,
        dynamic_watchlist,
        session_overrides,
        database,
    ):
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
        self.trading_agent     = trading_agent
        self.market_analyst    = market_analyst
        self.market_guard      = market_guard
        self.notifier          = notifier
        self.screener          = screener
        self.dynamic_watchlist = dynamic_watchlist
        self.session_overrides = session_overrides
        self.database          = database

        # Per-day session state
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

        self._state_lock = threading.Lock()

        self._ET = config.ET
        self._SCAN_TIMEOUT_SECONDS = 480

    def set_dry_run(self, flag: bool):
        """Args:
            flag: If True, run scans and log decisions but do not place orders.
        """
        self._dry_run = flag
        if flag:
            log.info("=== DRY-RUN MODE: market data and AI decisions will run, but NO real orders will be placed ===")

    def reset_daily_state(self):
        """Clear session counters, daily plan, guards; intended for each trading day.

        Returns:
            None.
        """
        self._deployed_today       = 0.0
        self._daily_pnl            = 0.0
        self._trades_today         = 0
        self._traded_buckets_today = set()
        self._session_date         = date.today().isoformat()
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

    def is_in_study_window(self, hour: int, minute: int) -> bool:
        """Args:
            hour: Current hour in ET (0–23).
            minute: Current minute (0–59).

        Returns:
            True if time is within the configured morning study window.
        """
        cur   = hour * 60 + minute
        start = config.STUDY_START_HOUR * 60 + config.STUDY_START_MIN
        end   = config.STUDY_END_HOUR   * 60 + config.STUDY_END_MIN
        return start <= cur < end

    def is_high_volume_window(self, hour: int, minute: int) -> bool:
        """Args:
            hour: ET hour.
            minute: ET minute.

        Returns:
            True if inside config.HIGH_VOLUME_WINDOWS.
        """
        cur = hour * 60 + minute
        for sh, sm, eh, em in config.HIGH_VOLUME_WINDOWS:
            if (sh * 60 + sm) <= cur <= (eh * 60 + em):
                return True
        return False

    def eod_close_all(self):
        """Flatten all positions at EOD; dry-run only logs.

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
        positions = self.broker.get_positions()
        for symbol, pos in positions.items():
            gfv_safe, reason = self.gfv_tracker.gfv_safe_to_sell(symbol)
            if not gfv_safe:
                log.warning("EOD: GFV block on %s — %s. Closing anyway (EOD mandatory).", symbol, reason)
            pnl           = float(getattr(pos, "unrealized_pl",  0) or 0)
            current_price = float(getattr(pos, "current_price",  0) or 0)
            qty           = float(getattr(pos, "qty",            0) or 0)
            self.broker.close_position(symbol)
            self.database.remove_position(symbol)
            self.gfv_tracker.remove_buy(symbol)
            self.database.record_decision(symbol, "SELL", price=current_price, qty=qty,
                            pnl=pnl, reasoning="EOD forced close — no overnight holds")
            self.database.update_outcome(symbol, "win" if pnl > 0 else "loss" if pnl < 0 else "breakeven", pnl)
            log.info("EOD closed %s | qty=%.0f price=%.2f pnl=%+.2f", symbol, qty, current_price, pnl)
        self.broker.cancel_all_orders()

    def write_daily_summary(self):
        """Persist DB summary, save dynamic watchlist carryover, notify.

        Returns:
            None.
        """
        today     = date.today().isoformat()
        all_dec   = self.database.get_recent_decisions(200)
        today_dec = [d for d in all_dec if d["ts"].startswith(today)]
        trades    = sum(1 for d in today_dec if d["action"] in ("BUY", "SELL", "PARTIAL_SELL"))
        wins      = sum(1 for d in today_dec if (d.get("pnl") or 0) > 0)
        losses    = sum(1 for d in today_dec if (d.get("pnl") or 0) < 0)
        gross     = sum((d.get("pnl") or 0) for d in today_dec)

        # Overall rolling expectancy
        exp_str  = self.expectancy_engine.expectancy_report(all_dec)
        exp_data = self.expectancy_engine.compute_expectancy(all_dec)

        # Setup-type breakdown for learning loop
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

        # Persist pre-Claude survivors as tomorrow's dynamic watchlist
        self.dynamic_watchlist.save(list(self._daily_pre_passed))

        # Send email notification
        self.notifier.send_daily_summary()

    def run_scan_and_trade(self):
        """Run _scan_body in a daemon thread with a wall-clock timeout.

        Returns:
            None.
        """
        t = threading.Thread(target=self._scan_body, daemon=True, name="scan-body")
        t.start()
        t.join(timeout=self._SCAN_TIMEOUT_SECONDS)
        if t.is_alive():
            log.error(
                "SCAN TIMEOUT after %ds — scan thread is stuck (hung API call?). "
                "Releasing scheduler lock so next 10-min cycle can start.",
                self._SCAN_TIMEOUT_SECONDS,
            )

