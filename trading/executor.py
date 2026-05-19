import config
from core.database import log
from trading.buy_executor import BuyExecutorMixin, _conviction_cap


class ExecutorMixin(BuyExecutorMixin):
    """Dispatch algo decisions and handle sells, stop updates, and dry-run logging."""

    def _handle_sell(
        self, d: dict, symbol: str, action: str, full_reason: str,
        open_symbols: set, positions_snapshot: list[dict], equity: float,
    ) -> dict | None:
        """Submit a market sell or half-size partial when GFV rules allow it.

        Args:
            d: Decision row with optional price hints.
            symbol: Uppercase ticker.
            action: SELL or PARTIAL_SELL string.
            full_reason: Stored reasoning text.
            open_symbols: Symbols currently open before this call.
            positions_snapshot: Snapshot rows for sizing and P and L.
            equity: Account equity for alerts.

        Returns:
            Dict with pnl, qty, and full_sell flag on success, otherwise None.
        """
        gfv_safe, gfv_reason = self.gfv_tracker.gfv_safe_to_sell(symbol)
        if not gfv_safe:
            self.database.record_decision(symbol, "SKIP", None,
                            reasoning=f"GFV block: {gfv_reason}", veto_rule="GFV_LOCK")
            log.warning("GFV block  cannot sell %s: %s", symbol, gfv_reason)
            return None

        pos_data      = next((p for p in positions_snapshot if p["symbol"] == symbol), {})
        current_price = pos_data.get("current_price") or d.get("price") or 0
        total_qty     = float(pos_data.get("qty", 0))
        entry_price   = pos_data.get("entry_price", 0)

        if action == "PARTIAL_SELL":
            if total_qty < 2:
                log.info("Partial sell skipped for %s  only %.0f share(s), cannot split", symbol, total_qty)
                return None
            qty = int(total_qty // 2)
            pnl = (current_price - entry_price) * qty if entry_price else 0
        else:
            qty = total_qty
            pnl = pos_data.get("pnl", 0.0)

        order = self.broker.place_market_order(symbol, qty, "SELL")
        if not order:
            return None

        with self._state_lock:
            self._daily_pnl += pnl

        setup_type = pos_data.get("setup_type") or d.get("setup_type")
        self.database.record_decision(symbol, action, current_price, qty,
                        pnl=pnl, reasoning=full_reason, setup_type=setup_type)

        if action == "PARTIAL_SELL":
            runner_qty  = float(pos_data.get("qty", 0)) - qty
            runner_stop = pos_data.get("stop_loss", 0)
            runner_tp   = pos_data.get("take_profit", 0)
            self.database.save_position(
                symbol, pos_data.get("entry_price", 0),
                runner_qty,
                runner_stop, runner_tp,
                trailing=pos_data.get("trailing", False),
                highest_price=pos_data.get("current_price"),
                partial_taken=True, entry_ts=pos_data.get("entry_ts", ""))
            # Resubmit broker protection for the runner  cancel stale bracket legs
            # (sized for old qty) then place a fresh bracket for the remaining shares.
            if runner_qty >= 1 and runner_stop and runner_tp:
                try:
                    self.broker.cancel_orders_for_symbol(symbol)
                    self.broker.place_bracket_order(
                        symbol, int(runner_qty), runner_stop, runner_tp)
                    log.info("Runner bracket resubmitted %s: qty=%.0f SL=%.2f TP=%.2f",
                             symbol, runner_qty, runner_stop, runner_tp)
                except Exception as _re:
                    log.warning("Could not resubmit runner bracket for %s: %s  submitting stop only", symbol, _re)
                    try:
                        self.broker.update_stop_loss(symbol, runner_stop)
                    except Exception:
                        pass
        else:
            self.database.remove_position(symbol)
            self.gfv_tracker.remove_buy(symbol)
            outcome = "win" if pnl > 0 else "loss" if pnl < 0 else "breakeven"
            self.database.update_outcome(symbol, outcome, pnl)

        positions_remaining = len(open_symbols) - (1 if action == "SELL" else 0)
        self.notifier.send_trade_alert(
            action=action, symbol=symbol, price=current_price, qty=qty,
            equity=equity, daily_pnl=self._daily_pnl,
            deployed=self._deployed_today, positions_open=positions_remaining,
            pnl=pnl, setup_type=setup_type, reason=full_reason,
        )
        return {"pnl": pnl, "qty": qty, "full_sell": action == "SELL"}

    def _handle_update_stop(
        self, d: dict, symbol: str, full_reason: str,
        positions_snapshot: list[dict],
    ) -> None:
        """Validate an UPDATE_STOP row and persist a new stop with the broker.

        Args:
            d: Decision dict containing stop_loss.
            symbol: Ticker to update.
            full_reason: Reason text stored on the decision row.
            positions_snapshot: Current snapshot for prior stop lookup.

        Returns:
            None.
        """
        new_stop = d.get("stop_loss")
        if not new_stop:
            return
        pos_data     = next((p for p in positions_snapshot if p["symbol"] == symbol), {})
        current_stop = pos_data.get("stop_loss", 0)
        ok, reason   = self.risk_manager.approve_stop_update(symbol, new_stop, current_stop)
        if not ok:
            log.info("UPDATE_STOP rejected %s: %s", symbol, reason)
            return
        self.broker.update_stop_loss(symbol, new_stop)
        self.database.save_position(
            symbol, pos_data.get("entry_price", 0), pos_data.get("qty", 0),
            new_stop, pos_data.get("take_profit", 0))
        self.database.record_decision(
            symbol, "UPDATE_STOP",
            pos_data.get("current_price") or d.get("price"),
            stop_loss=new_stop, reasoning=full_reason)

    def _log_dry_run(
        self, decisions: list[dict], settled_cash: float, equity: float,
        vix_factor: float, kelly_factor: float, _score_lookup: dict,
    ) -> None:
        """Print hypothetical sizing for each decision when dry-run mode is active.

        Args:
            decisions: Parsed model output rows.
            settled_cash: Snapshot buying power for calc_qty.
            equity: Snapshot equity.
            vix_factor: Regime multiplier passed through to sizing.
            kelly_factor: Kelly multiplier passed through to sizing.
            _score_lookup: Symbol to signal score map.

        Returns:
            None.
        """
        log.info("[DRY-RUN] Algo returned %d decisions  no orders will be placed:", len(decisions))
        for d in decisions:
            sym    = (d.get("symbol") or "?").upper()
            action = (d.get("action") or "SKIP").upper()
            final  = (d.get("final_decision") or "SKIP").upper()
            ep     = d.get("entry_price")
            sl     = d.get("stop_loss")
            tp     = d.get("take_profit")
            conf   = d.get("signal_confidence")
            reason = str(d.get("reason_for_entry") or "")[:90]

            qty  = None
            rr   = None
            risk = None
            if final == "BUY" or action == "BUY":
                try:
                    price = ep or self.broker.get_latest_price(sym)
                    if price:
                        df  = self.broker.get_bars(sym, "5Min", days=2)
                        df  = self.indicators.compute_indicators(df)
                        atr = float(df["atr"].iloc[-1]) if not df.empty else price * 0.01
                        rm_sl, rm_tp = self.risk_manager.compute_stop_take_profit(
                            price, atr, key_levels=self._key_levels_cache.get(sym))
                        if rm_sl and rm_tp:
                            sl = rm_sl
                            tp = rm_tp
                        _dry_score = float(_score_lookup.get(sym) or 0.0)
                        _dry_cap   = _conviction_cap(_dry_score, self._deployed_today)
                        qty_val = self.risk_manager.calc_qty(
                            price, sl, settled_cash, self._deployed_today,
                            equity, atr=atr, confidence=conf or 5,
                            vix_factor=vix_factor, kelly_factor=kelly_factor,
                            position_cap=_dry_cap)
                        qty  = qty_val if qty_val > 0 else None
                        if sl and tp and ep:
                            stop_dist   = ep - sl
                            reward_dist = tp - ep
                            rr   = round(reward_dist / stop_dist, 2) if stop_dist > 0 else None
                            risk = round(stop_dist * (qty or 0), 2) if qty else None
                except Exception as e:
                    log.debug("Dry-run level computation failed for %s: %s", sym, e)

            log.info("  [%s] %-6s  action=%-12s  entry=%-8s  SL=%-8s  TP=%-8s  "
                     "qty=%-5s  conf=%s  R:R=%s  risk=$%s",
                     final, sym, action,
                     f"{ep:.2f}" if ep else "",
                     f"{sl:.2f}" if sl else "",
                     f"{tp:.2f}" if tp else "",
                     qty or "", conf or "", rr or "", risk or "")
            if reason:
                log.info("         %s", reason)

    def execute_decisions(
        self,
        decisions: list[dict],
        positions_snapshot: list[dict],
        settled_cash: float,
        equity: float,
        effective_daily_pnl: float = 0.0,
        dynamic_confidence_bar: int = 0,
        vix_factor: float = 1.0,
        kelly_factor: float = 1.0,
        cooling_symbols: dict | None = None,
        suppressed_setups: dict | None = None,
        signal_score_lookup: dict | None = None,
        sector_strength: dict | None = None,
    ):
        """Dispatch BUY, SELL, PARTIAL_SELL, UPDATE_STOP, and SKIP/HOLD rows to handlers.

        Args:
            decisions: Parsed list of dicts from the algo decision engine.
            positions_snapshot: Output of build_positions_snapshot at scan time.
            settled_cash: Settled buying power snapshot; decremented locally on fills.
            equity: Account equity snapshot.
            effective_daily_pnl: Realized plus unrealized P and L for guards.
            dynamic_confidence_bar: Minimum confidence integer for new buys.
            vix_factor: Combined macro and intraday sizing multiplier.
            kelly_factor: Expectancy-derived Kelly multiplier.
            cooling_symbols: Optional map of symbol to human-readable skip reason.
            suppressed_setups: Optional map of setup type label to skip reason.
            signal_score_lookup: Optional map of symbol to numeric signal score.
            sector_strength: Optional map of sector bucket to strength label strings.

        Returns:
            None.
        """
        if dynamic_confidence_bar <= 0:
            dynamic_confidence_bar = config.MIN_SIGNAL_CONFIDENCE

        _score_lookup = signal_score_lookup or {}

        if self._dry_run:
            self._log_dry_run(decisions, settled_cash, equity, vix_factor, kelly_factor, _score_lookup)
            return

        open_symbols  = {p["symbol"] for p in positions_snapshot}
        num_positions = len(open_symbols)
        cooling       = cooling_symbols or {}
        suppressed    = suppressed_setups or {}

        for d in decisions:
            symbol = (d.get("symbol") or "").upper()
            action = (d.get("action") or "SKIP").upper()
            final  = (d.get("final_decision") or "SKIP").upper()
            _ss    = _score_lookup.get(symbol)

            reason_entry = d.get("reason_for_entry") or d.get("reasoning") or ""
            reason_avoid = d.get("reason_to_avoid") or ""
            full_reason  = reason_entry + (f" | AVOID: {reason_avoid}" if reason_avoid else "")

            if final == "SKIP" or action in ("SKIP", "HOLD"):
                self.database.record_decision(
                    symbol, action,
                    d.get("entry_price") or d.get("price"),
                    reasoning=full_reason, signal_score=_ss, veto_rule="AI_SKIP")
                continue

            if action == "BUY":
                cost = self._handle_buy(
                    d, symbol, _ss, full_reason, reason_entry,
                    open_symbols=open_symbols, num_positions=num_positions,
                    settled_cash=settled_cash, equity=equity,
                    effective_daily_pnl=effective_daily_pnl,
                    dynamic_confidence_bar=dynamic_confidence_bar,
                    vix_factor=vix_factor, kelly_factor=kelly_factor,
                    cooling_symbols=cooling, suppressed_setups=suppressed,
                    sector_strength=sector_strength,
                    positions_snapshot=positions_snapshot,
                    _score_lookup=_score_lookup,
                )
                if cost is not None:
                    settled_cash  -= cost
                    num_positions += 1
                    open_symbols.add(symbol)

            elif action in ("SELL", "PARTIAL_SELL"):
                if symbol not in open_symbols:
                    continue
                result = self._handle_sell(
                    d, symbol, action, full_reason,
                    open_symbols, positions_snapshot, equity)
                if result and result["full_sell"]:
                    open_symbols.discard(symbol)
                    num_positions -= 1

            elif action == "UPDATE_STOP":
                if symbol in open_symbols:
                    self._handle_update_stop(d, symbol, full_reason, positions_snapshot)
