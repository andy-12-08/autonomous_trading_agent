from datetime import date, datetime
import config
from core.database import log


class PositionsMixin:
    def build_positions_snapshot(self) -> list[dict]:
        """Sync broker vs DB, update stops/trailing/partials, prune closed rows.

        Returns:
            One dict per open position: symbol, bucket, prices, qty, pnl, stops,
            trailing, GFV flags, entry_ts, partial_taken, setup_type.
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

            # Move stop to breakeven at +BREAKEVEN_TRIGGER_PCT
            if self.risk_manager.should_move_to_breakeven(current_price, entry_price) and not trailing:
                breakeven = round(entry_price, 2)
                ok, _ = self.risk_manager.approve_stop_update(symbol, breakeven, stop_loss)
                if ok:
                    stop_loss = breakeven
                    self.broker.update_stop_loss(symbol, breakeven)
                    self.database.save_position(symbol, entry_price, qty, stop_loss, take_profit,
                                  trailing=False, highest_price=current_price)
                    self.database.record_decision(symbol, "UPDATE_STOP", current_price, qty,
                                    stop_loss=breakeven,
                                    reasoning="Stop moved to breakeven at +1.0% gain")

            # Activate trailing stop at +TRAILING_STOP_TRIGGER_PCT
            if self.risk_manager.should_trail(current_price, entry_price) and not trailing:
                new_stop = self.risk_manager.new_trailing_stop(current_price)
                ok, _ = self.risk_manager.approve_stop_update(symbol, new_stop, stop_loss)
                if ok:
                    stop_loss = new_stop
                    trailing  = True
                    self.broker.update_stop_loss(symbol, new_stop)
                    self.database.save_position(symbol, entry_price, qty, stop_loss, take_profit,
                                  trailing=True, highest_price=current_price)
                    self.database.record_decision(symbol, "UPDATE_STOP", current_price, qty,
                                    stop_loss=new_stop,
                                    reasoning="Trailing stop activated at +1.5% gain")

            # Ratchet trailing stop as price advances
            if trailing and current_price > highest:
                new_stop = self.risk_manager.new_trailing_stop(current_price)
                ok, _ = self.risk_manager.approve_stop_update(symbol, new_stop, stop_loss)
                if ok:
                    stop_loss = new_stop
                    self.broker.update_stop_loss(symbol, new_stop)
                    self.database.save_position(symbol, entry_price, qty, stop_loss, take_profit,
                                  trailing=True, highest_price=current_price)

            # Ratcheting take-profit: once price exceeds TP by 0.5%+, lock in a higher target.
            # Prevents leaving money on the table when a runner breaks out above the original TP.
            if take_profit > 0 and current_price > take_profit * 1.005:
                tp_range     = take_profit - entry_price
                new_tp       = round(current_price + tp_range * 0.5, 2)  # extend by 50% of original range
                if new_tp > take_profit:
                    take_profit = new_tp
                    self.database.save_position(symbol, entry_price, qty, stop_loss, take_profit,
                                  trailing=trailing, highest_price=current_price)
                    self.database.record_decision(symbol, "UPDATE_STOP", current_price, qty,
                                    stop_loss=stop_loss,
                                    reasoning=f"Ratcheting TP: price exceeded old target — new TP={new_tp:.2f}")

            # Stop-loss safety net: if no active stop order exists (e.g. after bot restart),
            # resubmit the DB stop so positions are never unprotected.
            if not self.broker.has_active_stop_order(symbol, open_orders):
                log.warning("No active stop order found for %s — resubmitting SL=%.2f",
                            symbol, stop_loss)
                self.broker.update_stop_loss(symbol, stop_loss)

            # GFV lock status
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

        # Prune DB entries that broker no longer holds
        # Position gone from broker = bracket stop/TP fired between cycles — capture the P&L
        for symbol in list(db_positions.keys()):
            if symbol not in broker_positions:
                db_pos      = db_positions[symbol]
                entry_price = float(db_pos.get("entry_price", 0) or 0)
                qty         = float(db_pos.get("qty",         0) or 0)
                setup_type  = db_pos.get("setup_type")

                fill = self.broker.get_last_filled_sell(symbol)
                try:
                    _bracket_equity = float(self.broker.get_account().equity or config.ACCOUNT_SIZE)
                except Exception:
                    _bracket_equity = config.ACCOUNT_SIZE
                if fill and fill["fill_price"]:
                    fill_price = fill["fill_price"]
                    pnl        = (fill_price - entry_price) * qty if entry_price else 0
                    outcome    = "win" if pnl > 0 else "loss" if pnl < 0 else "breakeven"
                    self.database.record_decision(symbol, "SELL", price=fill_price, qty=qty, pnl=pnl,
                                    setup_type=setup_type,
                                    reasoning="Bracket order triggered (stop-loss or take-profit hit by Alpaca)")
                    self.database.update_outcome(symbol, outcome, pnl)
                    self._daily_pnl += pnl
                    log.info("Bracket exit captured: %s | fill=%.2f entry=%.2f qty=%.0f pnl=%+.2f [%s]",
                             symbol, fill_price, entry_price, qty, pnl, outcome)
                    self.notifier.send_trade_alert(
                        action="SELL", symbol=symbol, price=fill_price, qty=qty,
                        equity=_bracket_equity, daily_pnl=self._daily_pnl,
                        pnl=pnl, setup_type=setup_type,
                        reason=f"Bracket exit [{outcome}] — stop-loss or take-profit triggered by Alpaca",
                    )
                else:
                    log.warning("Bracket exit for %s: fill data unavailable — recording without P&L", symbol)
                    self.database.record_decision(symbol, "SELL", price=entry_price, qty=qty,
                                    setup_type=setup_type,
                                    reasoning="Bracket order triggered — fill data unavailable")
                    self.notifier.send_trade_alert(
                        action="SELL", symbol=symbol, price=entry_price, qty=qty,
                        equity=_bracket_equity, daily_pnl=self._daily_pnl,
                        pnl=None, setup_type=setup_type,
                        reason="Bracket exit — stop-loss or take-profit triggered by Alpaca (fill data unavailable)",
                    )

                self.database.remove_position(symbol)
                self.gfv_tracker.remove_buy(symbol)

        return snapshot

    def check_time_stops(self, positions_snapshot: list[dict]) -> list[str]:
        """Identify positions that have exceeded the time-stop threshold.

        Institutional dead-trade rule: if a position has been open for
        TIME_STOP_MINUTES without reaching TIME_STOP_PROGRESS_PCT of its
        take-profit range, the thesis has failed — exit immediately.

        Args:
            positions_snapshot: Current positions list as returned by
                build_positions_snapshot().

        Returns:
            List of ticker symbols that must be exited immediately.
        """
        now     = datetime.now(self._ET)
        to_exit = []

        for pos in positions_snapshot:
            entry_ts_str = pos.get("entry_ts", "")
            if not entry_ts_str:
                continue
            try:
                entry_dt = datetime.fromisoformat(entry_ts_str).replace(tzinfo=self._ET)
            except Exception:
                continue

            age_minutes = (now - entry_dt).total_seconds() / 60
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
                    progress * 100, config.TIME_STOP_PROGRESS_PCT * 100,
                )
                to_exit.append(pos["symbol"])

        return to_exit

    def check_partial_profits(self, positions_snapshot: list[dict]) -> list[str]:
        """Identify positions eligible for automatic partial-profit scaling.

        Institutional scale-out rule: sell 50% of the position when price reaches
        PARTIAL_PROFIT_TRIGGER_PCT of the take-profit range from entry.

        Marks partial_taken=True in DB so this only fires once per position.

        Args:
            positions_snapshot: Current positions list as returned by
                build_positions_snapshot().

        Returns:
            List of ticker symbols to partial-sell.
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
                    pos["symbol"], progress * 100,
                )
                to_partial.append(pos["symbol"])
        return to_partial


    def run_position_management(self):
        """Fast-path scheduler job — fires every 2 minutes.

        Handles morning study, stop-losses, trailing stops, time stops, and
        partial profits. Never touches the universe scan or Claude — that is
        run_scan_and_trade()'s responsibility.
        """
        now   = datetime.now(self._ET)
        log.info("====[ POSITION MANAGEMENT | %s ]====", now.strftime("%H:%M:%S"))

        today = date.today().isoformat()

        if today != self._session_date:
            self.reset_daily_state()

        hour, minute = now.hour, now.minute

        # Allow pre-market study to run from 8:30 ET (before exchange opens at 9:30)
        in_premarket_study = self.is_in_study_window(hour, minute)
        if not self.broker.is_market_open() and not in_premarket_study:
            log.info("Market closed — skipping cycle")
            return

        # EOD close — runs exactly once per session
        if (hour == config.MARKET_CLOSE_HOUR and minute >= config.MARKET_CLOSE_MIN) or \
           hour > config.MARKET_CLOSE_HOUR:
            if not self._eod_done:
                self._eod_done = True
                self.eod_close_all()
                self.write_daily_summary()
            else:
                log.info("EOD already completed for today — skipping cycle")
            return

        # Too early — before study window
        cur_min     = hour * 60 + minute
        study_start = config.STUDY_START_HOUR * 60 + config.STUDY_START_MIN
        if cur_min < study_start:
            log.info("Pre-market — waiting for study window (%02d:%02d ET)",
                     config.STUDY_START_HOUR, config.STUDY_START_MIN)
            return

        # Gather account state (needed for both study and trading)
        broker_acct  = self.broker.get_account()
        equity       = float(getattr(broker_acct, "equity", None) or config.ACCOUNT_SIZE)
        # non_marginable_buying_power is settled cash on Alpaca cash accounts
        raw_settled  = float(getattr(broker_acct, "non_marginable_buying_power", None)
                             or getattr(broker_acct, "cash", None) or equity)
        settled_cash = self.gfv_tracker.get_available_settled_cash(raw_settled, self._deployed_today)
        exposure_pct = round(self._deployed_today / equity * 100, 1) if equity else 0

        account_ctx = {
            "settled_cash":          round(settled_cash, 2),
            "total_equity":          round(equity, 2),
            "daily_pnl_realized":    round(self._daily_pnl, 2),
            "daily_pnl_unrealized":  0.0,  # updated after positions_snapshot is built
            "daily_pnl_effective":   0.0,  # realized + unrealized (used for drawdown guard)
            "daily_pnl_pct":         0.0,
            "deployed_today":        round(self._deployed_today, 2),
            "total_exposure_pct":    exposure_pct,
            "available_today":       round(max(0, config.MAX_DAILY_CAPITAL - self._deployed_today), 2),
            "open_positions":        len(self.broker.get_positions()),
            "trades_today":          self._trades_today,
            "trades_remaining":      max(0, config.MAX_TRADES_PER_DAY - self._trades_today),
            "drawdown_limit":        config.DAILY_DRAWDOWN_LIMIT,
            "exposure_cap_pct":      int(config.MAX_TOTAL_EXPOSURE_PCT * 100),
            "max_daily_capital":     config.MAX_DAILY_CAPITAL,
        }

        # Morning Study (8:30–9:35 ET) — no trades
        in_study_window = self.is_in_study_window(hour, minute)

        if in_study_window and not self._study_complete:
            log.info("MORNING STUDY WINDOW (%02d:%02d) — studying market, no trades yet",
                     hour, minute)
            # Try to load a cached plan in case study already ran this session
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
            # Pre-warm caches so first trading cycle at 9:35 starts instantly
            log.info("Pre-warming screener universe cache...")
            self.screener.build_universe()
            log.info("Pre-loading FINRA dark pool data (yesterday's file)...")
            self.dark_pool.load_dark_pool_data()
            log.info("Pre-loading yield curve data...")
            self.yield_curve.get_yield_curve()
            log.info("Pre-loading pre-market levels for watchlist...")
            self.pre_market.get_premarket_data(config.WATCHLIST)
            log.info("All caches ready — first cycle will use cached data")
            return

        # Study window is active but already complete — wait for 9:35 before trading
        if in_study_window:
            log.info("Morning study done — waiting for %02d:%02d ET to begin trading",
                     config.STUDY_END_HOUR, config.STUDY_END_MIN)
            return

        # If we're past 9:35 and study hasn't run yet (e.g. bot started late), run it now
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

        # Trading cycles (9:35–15:44 ET)
        in_high_vol_window = self.is_high_volume_window(hour, minute)

        # Log posture from cached daily plan — no API calls, reads in-memory state only
        if self._daily_plan:
            posture = self._daily_plan.get("risk_posture", "normal")
            if posture in ("stand_aside", "conservative"):
                reason = (self._daily_plan.get("special_warnings") or ["macro/market conditions"])[0]
                log.warning("SESSION POSTURE: %s — %s", posture.upper(), reason[:120])

        log.info("--- POSITION MGMT %s | vol_window=%s pnl=%.0f deployed=%.0f (%.1f%%) trades=%d/%d ---",
                 now.strftime("%H:%M"), "YES" if in_high_vol_window else "MIDDAY",
                 self._daily_pnl, self._deployed_today, exposure_pct,
                 self._trades_today, config.MAX_TRADES_PER_DAY)

        positions_snapshot = self.build_positions_snapshot()

        # Effective PnL = realized + unrealized — used for drawdown guard
        unrealized_pnl      = sum(p.get("pnl", 0) for p in positions_snapshot)
        effective_daily_pnl = self._daily_pnl + unrealized_pnl

        # Warn at 1.5% effective drawdown
        if effective_daily_pnl <= -(equity * 0.015):
            log.warning("Drawdown warning: effective P&L $%.0f (%.1f%% of equity, "
                        "realized=%.0f unrealized=%.0f)",
                        effective_daily_pnl, abs(effective_daily_pnl / equity * 100),
                        self._daily_pnl, unrealized_pnl)

        # Time stops: exit dead positions that haven't moved
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
            if order:
                self._daily_pnl += pnl
                self.database.record_decision(sym, "SELL", pos_data.get("current_price"), qty,
                                pnl=pnl, setup_type=pos_data.get("setup_type"),
                                reasoning=f"Time stop: position aged > {config.TIME_STOP_MINUTES}min without reaching {config.TIME_STOP_PROGRESS_PCT:.0%} of target")
                self.database.remove_position(sym)
                self.gfv_tracker.remove_buy(sym)
                self.database.update_outcome(sym, "win" if pnl > 0 else "loss" if pnl < 0 else "breakeven", pnl)
                self.notifier.send_trade_alert(
                    action="SELL", symbol=sym,
                    price=float(pos_data.get("current_price") or 0), qty=qty,
                    equity=equity, daily_pnl=self._daily_pnl,
                    deployed=self._deployed_today,
                    positions_open=len(positions_snapshot) - 1,
                    pnl=pnl, setup_type="time_stop",
                    reason=f"Time stop: open >{config.TIME_STOP_MINUTES}min without {config.TIME_STOP_PROGRESS_PCT:.0%} progress",
                )

        # Auto partial profit: scale out 50% at 50% of take-profit
        partial_symbols = self.check_partial_profits(positions_snapshot)
        for sym in partial_symbols:
            gfv_safe, gfv_reason = self.gfv_tracker.gfv_safe_to_sell(sym)
            pos_data  = next((p for p in positions_snapshot if p["symbol"] == sym), {})
            qty       = float(pos_data.get("qty", 0))
            half_qty  = max(1, int(qty // 2))
            pnl       = float(pos_data.get("pnl", 0)) * (half_qty / qty) if qty else 0
            if not gfv_safe:
                log.info("Partial profit blocked by GFV for %s: %s", sym, gfv_reason)
                continue
            order = self.broker.place_market_order(sym, half_qty, "SELL")
            if order:
                self._daily_pnl += pnl
                self.database.record_decision(sym, "PARTIAL_SELL", pos_data.get("current_price"),
                                half_qty, pnl=pnl,
                                reasoning=f"Auto partial profit: reached {config.PARTIAL_PROFIT_TRIGGER_PCT:.0%} of take-profit range — scaling out 50%")
                # Runner gets a 1.5x TP extension — partial gains locked, let runner breathe
                orig_tp    = float(pos_data.get("take_profit", 0))
                entry      = float(pos_data.get("entry_price", 0))
                runner_tp  = round(entry + (orig_tp - entry) * 1.5, 2) if orig_tp > entry > 0 else orig_tp
                self.database.save_position(sym, pos_data["entry_price"], qty - half_qty,
                              pos_data["stop_loss"], runner_tp,
                              trailing=pos_data.get("trailing", False),
                              highest_price=pos_data.get("current_price"),
                              partial_taken=True,
                              entry_ts=pos_data.get("entry_ts", ""))
                if runner_tp != orig_tp:
                    log.info("Runner TP extended: %s orig=%.2f → runner=%.2f", sym, orig_tp, runner_tp)
                self.notifier.send_trade_alert(
                    action="PARTIAL_SELL", symbol=sym,
                    price=float(pos_data.get("current_price") or 0), qty=half_qty,
                    equity=equity, daily_pnl=self._daily_pnl,
                    deployed=self._deployed_today,
                    positions_open=len(positions_snapshot),
                    pnl=pnl, setup_type="auto_partial_profit",
                    reason=f"Auto scale-out: reached {config.PARTIAL_PROFIT_TRIGGER_PCT:.0%} of take-profit range",
                )

        # Refresh snapshot after time-stop and partial-profit exits
        positions_snapshot = self.build_positions_snapshot()
        log.info("Position management done — %d open position(s)", len(positions_snapshot))
        # Universe scan and Claude decisions are handled by run_scan_and_trade()

