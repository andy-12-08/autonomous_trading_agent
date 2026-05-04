from datetime import date, datetime, timezone
import config
from core.database import log


class PositionsMixin:
    """Two-minute job: reconcile broker and DB, manage stops, exits, and study phase."""

    def build_positions_snapshot(self) -> list[dict]:
        """Reconcile Alpaca positions with SQLite, update stops and targets, prune closed rows.

        Returns:
            List of one dict per open position with prices, sizing, stops, GFV flags,
            entry_ts, partial_taken, and setup_type fields for downstream logic.
        """
        broker_positions = self.broker.get_positions()
        db_positions     = {p["symbol"]: p for p in self.database.get_open_positions_db()}
        open_orders      = self.broker.get_open_orders()
        snapshot         = []

        for symbol, pos in broker_positions.items():
            current_price = float(pos.current_price   or 0)
            entry_price   = float(pos.avg_entry_price or 0)
            qty           = float(pos.qty             or 0)
            pnl           = float(pos.unrealized_pl   or 0)
            pnl_pct       = ((current_price - entry_price) / entry_price * 100) if entry_price else 0

            db          = db_positions.get(symbol, {})
            stop_loss   = db.get("stop_loss",   round(entry_price * (1 - config.DEFAULT_STOP_LOSS_PCT), 2))
            take_profit = db.get("take_profit", round(entry_price * (1 + config.DEFAULT_TAKE_PROFIT_PCT), 2))
            trailing    = bool(db.get("trailing", False))
            highest     = float(db.get("highest_price") or entry_price)
            stop_updated = False

            if entry_price > 0 and self.risk_manager.should_move_to_breakeven(current_price, entry_price) and not trailing:
                breakeven = round(entry_price, 2)
                ok, _ = self.risk_manager.approve_stop_update(symbol, breakeven, stop_loss)
                if ok:
                    stop_loss = breakeven
                    self.broker.update_stop_loss(symbol, breakeven)
                    stop_updated = True
                    self.database.save_position(symbol, entry_price, qty, stop_loss, take_profit,
                                  trailing=False, highest_price=current_price)
                    self.database.record_decision(symbol, "UPDATE_STOP", current_price, qty,
                                    stop_loss=breakeven,
                                    reasoning="Stop moved to breakeven at +1.0% gain")

            elif entry_price > 0 and self.risk_manager.should_trail(current_price, entry_price) and not trailing:
                new_stop = self.risk_manager.new_trailing_stop(current_price)
                ok, _ = self.risk_manager.approve_stop_update(symbol, new_stop, stop_loss)
                if ok:
                    stop_loss = new_stop
                    trailing  = True
                    self.broker.update_stop_loss(symbol, new_stop)
                    stop_updated = True
                    self.database.save_position(symbol, entry_price, qty, stop_loss, take_profit,
                                  trailing=True, highest_price=current_price)
                    self.database.record_decision(symbol, "UPDATE_STOP", current_price, qty,
                                    stop_loss=new_stop,
                                    reasoning="Trailing stop activated at +1.5% gain")

            if trailing and current_price > highest:
                new_stop = self.risk_manager.new_trailing_stop(current_price)
                ok, _ = self.risk_manager.approve_stop_update(symbol, new_stop, stop_loss)
                if ok:
                    stop_loss = new_stop
                    self.broker.update_stop_loss(symbol, new_stop)
                    stop_updated = True
                    self.database.save_position(symbol, entry_price, qty, stop_loss, take_profit,
                                  trailing=True, highest_price=current_price)

            if take_profit > 0 and current_price > take_profit * 1.005:
                tp_range = take_profit - entry_price
                new_tp   = round(current_price + tp_range * 0.5, 2)
                if new_tp > take_profit:
                    take_profit = new_tp
                    self.database.save_position(symbol, entry_price, qty, stop_loss, take_profit,
                                  trailing=trailing, highest_price=current_price)
                    self.database.record_decision(symbol, "UPDATE_STOP", current_price, qty,
                                    stop_loss=stop_loss,
                                    reasoning=f"Ratcheting TP: price exceeded old target — new TP={new_tp:.2f}")

            if not stop_updated and not self.broker.has_active_stop_order(symbol, open_orders):
                log.warning("No active stop order found for %s — resubmitting SL=%.2f",
                            symbol, stop_loss)
                self.broker.update_stop_loss(symbol, stop_loss)

            gfv_locked, gfv_reason = self.gfv_tracker.is_gfv_locked(symbol)
            snapshot.append({
                "symbol":        symbol,
                "bucket":        config.SYMBOL_BUCKET.get(symbol, "unknown"),
                "entry_price":   round(entry_price, 4),
                "current_price": round(current_price, 4),
                "qty":           qty,
                "stop_loss":     round(stop_loss, 4),
                "take_profit":   round(take_profit, 4),
                "pnl":           round(pnl, 2),
                "pnl_pct":       round(pnl_pct, 2),
                "trailing":      trailing,
                "gfv_locked":    gfv_locked,
                "gfv_reason":    gfv_reason,
                "entry_ts":      db.get("entry_ts", ""),
                "partial_taken": bool(db.get("partial_taken", False)),
                "setup_type":    db.get("setup_type"),
            })

        for symbol in list(db_positions.keys()):
            if symbol not in broker_positions:
                self._capture_bracket_exit(symbol, db_positions[symbol])

        return snapshot

    def _capture_bracket_exit(self, symbol: str, db_pos: dict) -> None:
        """Record P and L and DB cleanup when a bracket exit happens between scheduler ticks.

        Args:
            symbol: Ticker that disappeared from the broker position list.
            db_pos: Last known SQLite row for that symbol.

        Returns:
            None.
        """
        entry_price = float(db_pos.get("entry_price", 0) or 0)
        qty         = float(db_pos.get("qty",         0) or 0)
        setup_type  = db_pos.get("setup_type")

        try:
            _bracket_equity = float(self.broker.get_account().equity or config.ACCOUNT_SIZE)
        except Exception:
            _bracket_equity = config.ACCOUNT_SIZE

        fill = self.broker.get_last_filled_sell(symbol)
        if fill and fill["fill_price"]:
            fill_price = fill["fill_price"]
            pnl        = (fill_price - entry_price) * qty if entry_price else 0
            outcome    = "win" if pnl > 0 else "loss" if pnl < 0 else "breakeven"
            self.database.record_decision(
                symbol, "SELL", price=fill_price, qty=qty, pnl=pnl,
                setup_type=setup_type,
                reasoning="Bracket order triggered (stop-loss or take-profit hit by Alpaca)")
            self.database.update_outcome(symbol, outcome, pnl)
            with self._state_lock:
                self._daily_pnl += pnl
            log.info("Bracket exit captured: %s | fill=%.2f entry=%.2f qty=%.0f pnl=%+.2f [%s]",
                     symbol, fill_price, entry_price, qty, pnl, outcome)
            self.notifier.send_trade_alert(
                action="SELL", symbol=symbol, price=fill_price, qty=qty,
                equity=_bracket_equity, daily_pnl=self._daily_pnl,
                pnl=pnl, setup_type=setup_type,
                reason=f"Bracket exit [{outcome}] — stop-loss or take-profit triggered by Alpaca")
        else:
            log.warning("Bracket exit for %s: fill data unavailable — recording without P&L", symbol)
            self.database.record_decision(
                symbol, "SELL", price=entry_price, qty=qty,
                setup_type=setup_type,
                reasoning="Bracket order triggered — fill data unavailable")
            self.notifier.send_trade_alert(
                action="SELL", symbol=symbol, price=entry_price, qty=qty,
                equity=_bracket_equity, daily_pnl=self._daily_pnl,
                pnl=None, setup_type=setup_type,
                reason="Bracket exit — stop-loss or take-profit triggered by Alpaca (fill data unavailable)")

        self.database.remove_position(symbol)
        self.gfv_tracker.remove_buy(symbol)

    def check_time_stops(self, positions_snapshot: list[dict]) -> list[str]:
        """Return symbols that exceeded the configured time stop without enough TP progress.

        Args:
            positions_snapshot: Rows from build_positions_snapshot including entry_ts.

        Returns:
            List of ticker strings that should receive a time-stop exit order.
        """
        now     = datetime.now(self._ET)
        to_exit = []

        for pos in positions_snapshot:
            entry_ts_str = pos.get("entry_ts", "")
            if not entry_ts_str:
                continue
            try:
                entry_dt = datetime.fromisoformat(entry_ts_str)
                if entry_dt.tzinfo is None:
                    entry_dt = entry_dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            age_minutes = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60
            if age_minutes < config.TIME_STOP_MINUTES:
                continue

            entry    = pos["entry_price"]
            tp       = pos["take_profit"]
            current  = pos["current_price"]
            tp_range = tp - entry
            if tp_range <= 0:
                continue

            progress = (current - entry) / tp_range
            if progress < config.TIME_STOP_PROGRESS_PCT:
                log.warning(
                    "TIME STOP: %s open %.0f min, progress=%.0f%% < %.0f%% target — exiting",
                    pos["symbol"], age_minutes,
                    progress * 100, config.TIME_STOP_PROGRESS_PCT * 100)
                to_exit.append(pos["symbol"])

        return to_exit

    def check_partial_profits(self, positions_snapshot: list[dict]) -> list[str]:
        """Return symbols eligible for an automatic half-size take-profit per config rules.

        Args:
            positions_snapshot: Rows from build_positions_snapshot; skips partial_taken.

        Returns:
            List of tickers to scale out when price has covered enough of the TP range.
        """
        to_partial = []
        for pos in positions_snapshot:
            if pos.get("partial_taken"):
                continue
            entry    = pos["entry_price"]
            tp       = pos["take_profit"]
            current  = pos["current_price"]
            tp_range = tp - entry
            if tp_range <= 0:
                continue
            progress = (current - entry) / tp_range
            if progress >= config.PARTIAL_PROFIT_TRIGGER_PCT:
                log.info(
                    "PARTIAL PROFIT: %s at %.0f%% of take-profit range — selling 50%%",
                    pos["symbol"], progress * 100)
                to_partial.append(pos["symbol"])
        return to_partial

    def _run_study_phase(self, hour: int, minute: int, account_ctx: dict) -> bool:
        """Run or load the morning study during the configured pre-open window.

        Args:
            hour: Current Eastern Time hour.
            minute: Current Eastern Time minute.
            account_ctx: Account summary dict passed into the analyst.

        Returns:
            True when the caller should stop the rest of this position-management tick.
        """
        in_study_window = self.is_in_study_window(hour, minute)

        if in_study_window and not self._study_complete:
            log.info("MORNING STUDY WINDOW (%02d:%02d) — studying market, no trades yet", hour, minute)
            cached = self.market_analyst.load_todays_plan()
            if cached:
                self._daily_plan    = cached
                self._study_complete = True
                log.info("Loaded cached daily plan from DB")
            else:
                self._daily_plan    = self.market_analyst.run_morning_study(account_ctx)
                self._study_complete = True
            self.session_overrides.apply(self._daily_plan)
            log.info("Session overrides: %s", self.session_overrides.summary())
            log.info("Pre-warming screener universe cache...")
            self.screener.build_universe()
            log.info("Pre-loading FINRA dark pool data (yesterday's file)...")
            self.dark_pool.load_dark_pool_data()
            log.info("Pre-loading yield curve data...")
            self.yield_curve.get_yield_curve()
            log.info("Pre-loading pre-market levels for watchlist...")
            self.pre_market.get_premarket_data(config.WATCHLIST)
            log.info("All caches ready — first cycle will use cached data")
            return True

        if in_study_window:
            log.info("Morning study done — waiting for %02d:%02d ET to begin trading",
                     config.STUDY_END_HOUR, config.STUDY_END_MIN)
            return True

        if not self._study_complete:
            cached = self.market_analyst.load_todays_plan()
            if cached:
                self._daily_plan    = cached
                self._study_complete = True
                log.info("Loaded cached daily plan (late start)")
            else:
                log.info("Running morning study (late start — %02d:%02d)", hour, minute)
                self._daily_plan    = self.market_analyst.run_morning_study(account_ctx)
                self._study_complete = True
            self.session_overrides.apply(self._daily_plan)
            log.info("Session overrides: %s", self.session_overrides.summary())

        return False

    def _execute_time_stop_exits(self, positions_snapshot: list[dict], equity: float) -> None:
        """Place market sells for symbols returned by check_time_stops when GFV allows.

        Args:
            positions_snapshot: Snapshot used to size each exit.
            equity: Current account equity for alerts.

        Returns:
            None.
        """
        time_stop_exits = self.check_time_stops(positions_snapshot)
        for sym in time_stop_exits:
            gfv_safe, gfv_reason = self.gfv_tracker.gfv_safe_to_sell(sym)
            pos_data = next((p for p in positions_snapshot if p["symbol"] == sym), {})
            qty      = float(pos_data.get("qty", 0))
            pnl      = float(pos_data.get("pnl", 0))
            if not gfv_safe:
                log.warning("Time stop blocked by GFV for %s: %s", sym, gfv_reason)
                continue
            order = self.broker.place_market_order(sym, qty, "SELL")
            if not order:
                continue
            with self._state_lock:
                self._daily_pnl += pnl
            self.database.record_decision(
                sym, "SELL", pos_data.get("current_price"), qty,
                pnl=pnl, setup_type=pos_data.get("setup_type"),
                reasoning=(f"Time stop: position aged > {config.TIME_STOP_MINUTES}min "
                           f"without reaching {config.TIME_STOP_PROGRESS_PCT:.0%} of target"))
            self.database.remove_position(sym)
            self.gfv_tracker.remove_buy(sym)
            self.database.update_outcome(
                sym, "win" if pnl > 0 else "loss" if pnl < 0 else "breakeven", pnl)
            self.notifier.send_trade_alert(
                action="SELL", symbol=sym,
                price=float(pos_data.get("current_price") or 0), qty=qty,
                equity=equity, daily_pnl=self._daily_pnl,
                deployed=self._deployed_today,
                positions_open=len(positions_snapshot) - 1,
                pnl=pnl, setup_type="time_stop",
                reason=f"Time stop: open >{config.TIME_STOP_MINUTES}min "
                       f"without {config.TIME_STOP_PROGRESS_PCT:.0%} progress")

    def _execute_partial_profit_exits(self, positions_snapshot: list[dict], equity: float) -> None:
        """Sell half of each eligible position (skips single-share lines) when GFV allows.

        Args:
            positions_snapshot: Snapshot used for sizing and follow-up saves.
            equity: Current account equity for alerts.

        Returns:
            None.
        """
        partial_symbols = self.check_partial_profits(positions_snapshot)
        for sym in partial_symbols:
            gfv_safe, gfv_reason = self.gfv_tracker.gfv_safe_to_sell(sym)
            pos_data = next((p for p in positions_snapshot if p["symbol"] == sym), {})
            qty = float(pos_data.get("qty", 0))
            if qty < 2:
                log.info("Partial profit skipped for %s — only %.0f share(s), cannot split", sym, qty)
                continue
            half_qty = int(qty // 2)
            pnl      = float(pos_data.get("pnl", 0)) * (half_qty / qty)
            if not gfv_safe:
                log.info("Partial profit blocked by GFV for %s: %s", sym, gfv_reason)
                continue
            order = self.broker.place_market_order(sym, half_qty, "SELL")
            if not order:
                continue
            with self._state_lock:
                self._daily_pnl += pnl
            self.database.record_decision(
                sym, "PARTIAL_SELL", pos_data.get("current_price"), half_qty, pnl=pnl,
                reasoning=(f"Auto partial profit: reached {config.PARTIAL_PROFIT_TRIGGER_PCT:.0%} "
                           f"of take-profit range — scaling out 50%"))
            orig_tp   = float(pos_data.get("take_profit", 0))
            entry     = float(pos_data.get("entry_price", 0))
            runner_tp = round(entry + (orig_tp - entry) * 1.5, 2) if orig_tp > entry > 0 else orig_tp
            self.database.save_position(
                sym, pos_data["entry_price"], qty - half_qty,
                pos_data["stop_loss"], runner_tp,
                trailing=pos_data.get("trailing", False),
                highest_price=pos_data.get("current_price"),
                partial_taken=True, entry_ts=pos_data.get("entry_ts", ""))
            if runner_tp != orig_tp:
                log.info("Runner TP extended: %s orig=%.2f → runner=%.2f", sym, orig_tp, runner_tp)
            self.notifier.send_trade_alert(
                action="PARTIAL_SELL", symbol=sym,
                price=float(pos_data.get("current_price") or 0), qty=half_qty,
                equity=equity, daily_pnl=self._daily_pnl,
                deployed=self._deployed_today,
                positions_open=len(positions_snapshot),
                pnl=pnl, setup_type="auto_partial_profit",
                reason=f"Auto scale-out: reached {config.PARTIAL_PROFIT_TRIGGER_PCT:.0%} of take-profit range")

    def run_position_management(self):
        """Scheduler entry: study window, EOD flattening, then intraday position upkeep.

        Returns:
            None.
        """
        now   = datetime.now(self._ET)
        log.info("====[ POSITION MANAGEMENT | %s ]====", now.strftime("%H:%M:%S"))

        today = datetime.now(self._ET).date().isoformat()
        if today != self._session_date:
            self.reset_daily_state()

        hour, minute = now.hour, now.minute

        if self._force_run:
            log.info("POSITION MGMT: force mode — bypassing market-hours gates")
        else:
            in_premarket_study = self.is_in_study_window(hour, minute)
            if not self.broker.is_market_open() and not in_premarket_study:
                log.info("Market closed — skipping cycle")
                return

            if (hour == config.MARKET_CLOSE_HOUR and minute >= config.MARKET_CLOSE_MIN) or \
               hour > config.MARKET_CLOSE_HOUR:
                if not self._eod_done:
                    try:
                        self.eod_close_all()
                        self._eod_done = True
                    except Exception as exc:
                        log.error("EOD close failed: %s — will retry on next tick", exc)
                        return
                    try:
                        self.write_daily_summary()
                    except Exception as exc:
                        log.error("EOD summary failed: %s", exc)
                else:
                    log.info("EOD already completed for today — skipping cycle")
                return

            cur_min     = hour * 60 + minute
            study_start = config.STUDY_START_HOUR * 60 + config.STUDY_START_MIN
            if cur_min < study_start:
                log.info("Pre-market — waiting for study window (%02d:%02d ET)",
                         config.STUDY_START_HOUR, config.STUDY_START_MIN)
                return

        broker_acct  = self.broker.get_account()
        equity       = float(getattr(broker_acct, "equity", None) or config.ACCOUNT_SIZE)
        raw_settled  = float(getattr(broker_acct, "non_marginable_buying_power", None)
                             or getattr(broker_acct, "cash", None) or equity)
        settled_cash = self.gfv_tracker.get_available_settled_cash(raw_settled, self._deployed_today)
        exposure_pct = round(self._deployed_today / equity * 100, 1) if equity else 0

        account_ctx = {
            "settled_cash":         round(settled_cash, 2),
            "total_equity":         round(equity, 2),
            "daily_pnl_realized":   round(self._daily_pnl, 2),
            "daily_pnl_unrealized": 0.0,
            "daily_pnl_effective":  0.0,
            "daily_pnl_pct":        0.0,
            "deployed_today":       round(self._deployed_today, 2),
            "total_exposure_pct":   exposure_pct,
            "available_today":      round(max(0, config.MAX_DAILY_CAPITAL - self._deployed_today), 2),
            "open_positions":       len(self.broker.get_positions()),
            "trades_today":         self._trades_today,
            "trades_remaining":     max(0, config.MAX_TRADES_PER_DAY - self._trades_today),
            "drawdown_limit":       config.DAILY_DRAWDOWN_LIMIT,
            "exposure_cap_pct":     int(config.MAX_TOTAL_EXPOSURE_PCT * 100),
            "max_daily_capital":    config.MAX_DAILY_CAPITAL,
        }

        if self._run_study_phase(hour, minute, account_ctx):
            return

        in_high_vol_window = self.is_high_volume_window(hour, minute)

        if self._daily_plan:
            posture = self._daily_plan.get("risk_posture", "normal")
            if posture in ("stand_aside", "conservative"):
                reason = (self._daily_plan.get("special_warnings") or ["macro/market conditions"])[0]
                log.warning("SESSION POSTURE: %s — %s", posture.upper(), reason[:120])

        log.info("--- POSITION MGMT %s | vol_window=%s pnl=%.0f deployed=%.0f (%.1f%%) trades=%d/%d ---",
                 now.strftime("%H:%M"), "YES" if in_high_vol_window else "MIDDAY",
                 self._daily_pnl, self._deployed_today, exposure_pct,
                 self._trades_today, config.MAX_TRADES_PER_DAY)

        with self._broker_lock:
            positions_snapshot = self.build_positions_snapshot()

        unrealized_pnl      = sum(p.get("pnl", 0) for p in positions_snapshot)
        effective_daily_pnl = self._daily_pnl + unrealized_pnl

        if effective_daily_pnl <= -(equity * 0.015):
            log.warning("Drawdown warning: effective P&L $%.0f (%.1f%% of equity, "
                        "realized=%.0f unrealized=%.0f)",
                        effective_daily_pnl, abs(effective_daily_pnl / equity * 100),
                        self._daily_pnl, unrealized_pnl)

        self._execute_time_stop_exits(positions_snapshot, equity)
        self._execute_partial_profit_exits(positions_snapshot, equity)

        positions_snapshot = self.build_positions_snapshot()
        log.info("Position management done — %d open position(s)", len(positions_snapshot))
