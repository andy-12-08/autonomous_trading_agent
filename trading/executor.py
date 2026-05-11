from datetime import datetime
import config
from core.database import log


def _safe_float(v, default: float) -> float:
    """Convert v to float, returning default on None, non-numeric strings, or any error."""
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _conviction_cap(signal_score: float, deployed_today: float) -> float:
    """Conviction-tier dollar cap for a new trade after prior deployments today.

    Args:
        signal_score: Composite signal score used to pick a CONVICTION_TIERS row.
        deployed_today: Capital already committed in the session.

    Returns:
        Maximum dollars allowed for this idea before other caps apply.
    """
    remaining = config.MAX_DAILY_CAPITAL - deployed_today
    fraction  = config.CONVICTION_TIERS[-1][1]
    for min_score, frac in config.CONVICTION_TIERS:
        if signal_score >= min_score:
            fraction = frac
            break
    return min(config.MAX_DAILY_CAPITAL * fraction, remaining)


class ExecutorMixin:
    """Apply algo decisions through risk checks and Alpaca order helpers."""

    def _handle_buy(
        self, d: dict, symbol: str, _ss, full_reason: str, reason_entry: str,
        open_symbols: set, num_positions: int, settled_cash: float, equity: float,
        effective_daily_pnl: float, dynamic_confidence_bar: int,
        vix_factor: float, kelly_factor: float,
        cooling_symbols: dict, suppressed_setups: dict,
        sector_strength: dict | None, positions_snapshot: list[dict],
        _score_lookup: dict,
    ) -> float | None:
        """Validate a BUY row, size it, and submit a bracket order when checks pass.

        Args:
            d: Raw decision dict from the model or fallback.
            symbol: Uppercase ticker.
            _ss: Programmatic signal score for logging vetoes.
            full_reason: Combined reason string stored on decisions.
            reason_entry: Short rationale for notifications.
            open_symbols: Mutable set of symbols currently held.
            num_positions: Current open count before this BUY.
            settled_cash: Settled buying power snapshot passed from the scan.
            equity: Account equity snapshot.
            effective_daily_pnl: Realized plus unrealized guard input.
            dynamic_confidence_bar: Minimum confidence for entries.
            vix_factor: Regime sizing multiplier.
            kelly_factor: Kelly sizing multiplier.
            cooling_symbols: Map of symbol to cooling reason.
            suppressed_setups: Map of setup type to suppression reason.
            sector_strength: Sector strength labels for bucket logic.
            positions_snapshot: Open positions list used for heat and buckets.
            _score_lookup: Map of symbol to signal score.

        Returns:
            Fill cost in dollars for the caller to subtract from settled_cash, or None.
        """
        if symbol in open_symbols:
            log.info("Skip BUY %s — already holding", symbol)
            return None

        if effective_daily_pnl <= -config.DAILY_DRAWDOWN_LIMIT:
            self.database.record_decision(symbol, "SKIP", d.get("entry_price"),
                            reasoning=f"Daily drawdown limit (${effective_daily_pnl:.0f} realized+unrealized). Rule 6.",
                            signal_score=_ss, veto_rule="DRAWDOWN_LIMIT")
            log.warning("Daily drawdown guard — no new buys. effective_pnl=%.0f", effective_daily_pnl)
            return None

        posture = (self._daily_plan or {}).get("risk_posture", "normal")
        if posture == "stand_aside":
            self.database.record_decision(symbol, "SKIP", d.get("entry_price"),
                            reasoning="Morning study: stand_aside — no new entries today",
                            signal_score=_ss, veto_rule="STAND_ASIDE")
            return None

        setup_type_hint = d.get("setup_type") or d.get("setup_type_hint") or ""
        if suppressed_setups and setup_type_hint and setup_type_hint in suppressed_setups:
            self.database.record_decision(symbol, "SKIP", d.get("entry_price"),
                            reasoning=suppressed_setups[setup_type_hint],
                            signal_score=_ss, veto_rule="SETUP_SUPPRESSED")
            log.info("SETUP SUPPRESSED %s [%s]: %s",
                     symbol, setup_type_hint, suppressed_setups[setup_type_hint][:80])
            return None

        if cooling_symbols and symbol in cooling_symbols:
            self.database.record_decision(symbol, "SKIP", d.get("entry_price"),
                            reasoning=cooling_symbols[symbol],
                            signal_score=_ss, veto_rule="COOLING")
            log.info("COOLING veto %s: %s", symbol, cooling_symbols[symbol])
            return None

        eb_blocked, eb_reason = self.market_guard.is_earnings_blackout(symbol)
        if eb_blocked:
            self.database.record_decision(symbol, "SKIP", d.get("entry_price"),
                            reasoning=eb_reason, signal_score=_ss, veto_rule="EARNINGS_BLACKOUT")
            return None

        confidence = int(float(d.get("signal_confidence") or 0))
        consec     = self.expectancy_engine.get_recent_consecutive_losses(
                         self.database.get_recent_decisions(40))
        rtg_ok, rtg_reason = self.expectancy_engine.check_revenge_trade_guard(consec, confidence)
        if not rtg_ok:
            self.database.record_decision(symbol, "SKIP", d.get("entry_price"),
                            reasoning=f"Revenge-trade guard: {rtg_reason}",
                            signal_score=_ss, veto_rule="REVENGE_TRADE")
            log.warning("REVENGE-TRADE guard %s: %s", symbol, rtg_reason)
            return None

        if confidence < dynamic_confidence_bar:
            self.database.record_decision(symbol, "SKIP", d.get("entry_price"),
                            reasoning=(f"Dynamic confidence bar: need {dynamic_confidence_bar}/10 "
                                       f"(recent form weak), got {confidence}/10"),
                            signal_score=_ss, veto_rule="DYN_CONFIDENCE")
            log.info("Dynamic bar blocked %s: conf=%d < bar=%d",
                     symbol, confidence, dynamic_confidence_bar)
            return None

        price = d.get("entry_price") or d.get("price") or self.broker.get_latest_price(symbol)
        if not price:
            return None

        stop_loss   = d.get("stop_loss")
        take_profit = d.get("take_profit")

        try:
            df  = self.broker.get_bars(symbol, "5Min", days=2)
            df  = self.indicators.compute_indicators(df)
            atr = float(df["atr"].iloc[-1]) if not df.empty else price * 0.01
            sig = self.indicators.get_signal_summary(df) if not df.empty else {}
        except Exception as e:
            log.warning("Could not fetch bars/indicators for %s: %s — using ATR fallback", symbol, e)
            atr = price * 0.01
            sig = {}

        if self.risk_manager.is_too_volatile(atr, price):
            self.database.record_decision(symbol, "SKIP", price,
                            reasoning=f"ATR too high ({atr/price:.1%}) — skip (Rule 5)",
                            signal_score=_ss, veto_rule="ATR_TOO_HIGH")
            log.info("ATR too high for %s — skipping", symbol)
            return None

        rm_sl, rm_tp = self.risk_manager.compute_stop_take_profit(
            price, atr, key_levels=self._key_levels_cache.get(symbol))
        if rm_sl and rm_tp:
            if stop_loss and take_profit:
                log.debug("Overriding decision SL=%.2f/TP=%.2f with risk-manager SL=%.2f/TP=%.2f",
                          stop_loss, take_profit, rm_sl, rm_tp)
            stop_loss, take_profit = rm_sl, rm_tp
        elif not stop_loss or not take_profit:
            stop_loss   = float(rm_sl or stop_loss or price * 0.98)
            take_profit = float(rm_tp or take_profit or price * 1.04)
        if not stop_loss or not take_profit:
            self.database.record_decision(symbol, "SKIP", price,
                            reasoning="Could not compute valid SL/TP — skipping",
                            signal_score=_ss, veto_rule="NO_LEVELS")
            return None

        bucket_ok, bucket_reason = self.bucket_manager.bucket_is_open(
            symbol, positions_snapshot, confidence, sector_strength=sector_strength)
        if not bucket_ok:
            self.database.record_decision(symbol, "SKIP", price,
                            reasoning=f"Bucket veto: {bucket_reason}",
                            signal_score=_ss, veto_rule="BUCKET")
            log.info("BUCKET veto %s: %s", symbol, bucket_reason)
            return None

        corr_ok, corr_reason = self.market_guard.check_correlation(symbol, positions_snapshot)
        if not corr_ok:
            self.database.record_decision(symbol, "SKIP", price,
                            reasoning=corr_reason, signal_score=_ss, veto_rule="CORRELATION")
            log.info("CORRELATION veto %s: %s", symbol, corr_reason)
            return None

        _sym_score     = float(_score_lookup.get(symbol) or 0.0)
        conviction_cap = _conviction_cap(_sym_score, self._deployed_today)
        if conviction_cap <= 0:
            self.database.record_decision(symbol, "SKIP", price,
                            reasoning="Daily capital exhausted — conviction cap below minimum",
                            signal_score=_ss, veto_rule="QTY_ZERO")
            log.info("Daily capital exhausted for %s — skipping", symbol)
            return None
        log.info("Conviction cap %s: score=%.1f → $%.0f (%.0f%% of $%.0f daily cap)",
                 symbol, _sym_score, conviction_cap,
                 conviction_cap / config.MAX_DAILY_CAPITAL * 100, config.MAX_DAILY_CAPITAL)

        qty = self.risk_manager.calc_qty(
            price, stop_loss, settled_cash, self._deployed_today,
            equity, atr=atr, confidence=confidence,
            vix_factor=vix_factor, kelly_factor=kelly_factor,
            position_cap=conviction_cap)
        if qty <= 0:
            self.database.record_decision(symbol, "SKIP", price,
                            reasoning="qty=0 after vol-adjusted sizing",
                            signal_score=_ss, veto_rule="QTY_ZERO")
            return None

        new_risk = (price - stop_loss) * qty if stop_loss else 0
        heat_ok, heat_reason = self.risk_manager.check_portfolio_heat(
            positions_snapshot, new_risk, equity)
        if not heat_ok:
            self.database.record_decision(symbol, "SKIP", price,
                            reasoning=heat_reason, signal_score=_ss, veto_rule="PORTFOLIO_HEAT")
            log.warning("PORTFOLIO HEAT veto %s: %s", symbol, heat_reason)
            return None

        rr        = _safe_float(d.get("reward_to_risk"), 0.0)
        vol_ratio = _safe_float(sig.get("rvol") or sig.get("vol_ratio") or d.get("vol_ratio"), 0.0)
        rsi       = _safe_float(sig.get("rsi")       or d.get("rsi"),       50.0)

        _now_et    = datetime.now(self._ET)
        _cur_min   = _now_et.hour * 60 + _now_et.minute
        _open_min  = config.MARKET_OPEN_HOUR * 60 + config.MARKET_OPEN_MIN
        _early_end = config.EARLY_WINDOW_END_HOUR * 60 + config.EARLY_WINDOW_END_MIN
        _in_early  = _open_min <= _cur_min < _early_end
        _gap_pct   = float(sig.get("gap_pct", 0))
        _gap_go    = _in_early and _gap_pct >= config.GAP_AND_GO_MIN_VOL_PCT and bool(sig.get("above_vwap"))

        if _gap_go:
            _vol_floor = config.GAP_AND_GO_VOL_RATIO
            log.info("Gap-and-go early entry %s: gap=%.1f%% above_vwap=True — vol floor relaxed to %.1f",
                     symbol, _gap_pct, config.GAP_AND_GO_VOL_RATIO)
        elif _in_early:
            _vol_floor = config.EARLY_WINDOW_VOL_RATIO
            log.info("Early window %s: vol floor relaxed to %.1f (was %.1f)",
                     symbol, config.EARLY_WINDOW_VOL_RATIO, config.MIN_VOL_RATIO_ENTRY)
        else:
            _vol_floor = None

        # ── Entry time gate ───────────────────────────────────────────────────────
        _prime_end = config.PRIME_ENTRY_END_HOUR * 60 + config.PRIME_ENTRY_END_MIN
        _close_min = config.MARKET_CLOSE_HOUR    * 60 + config.MARKET_CLOSE_MIN

        if _cur_min >= _close_min:
            self.database.record_decision(symbol, "SKIP", price,
                            reasoning="Late-day gate: no new entries after 3:45 PM ET",
                            signal_score=_ss, veto_rule="LATE_DAY_GATE")
            log.info("Late-day gate: no new entries after 3:45 ET — skip %s", symbol)
            return None

        if _cur_min > _prime_end:
            _conf = int(d.get("signal_confidence") or d.get("confidence") or 0)
            if _ss < config.MIDDAY_ENTRY_MIN_SCORE or _conf < config.MIDDAY_ENTRY_MIN_CONF:
                self.database.record_decision(symbol, "SKIP", price,
                                reasoning=(f"Midday gate: score {_ss:.1f}<{config.MIDDAY_ENTRY_MIN_SCORE} "
                                           f"or conf {_conf}<{config.MIDDAY_ENTRY_MIN_CONF} outside prime window"),
                                signal_score=_ss, veto_rule="MIDDAY_GATE")
                log.info("Midday gate %s: score=%.1f conf=%d — need ≥%.1f/≥%d outside 9:30–10:15 prime window",
                         symbol, _ss, _conf, config.MIDDAY_ENTRY_MIN_SCORE, config.MIDDAY_ENTRY_MIN_CONF)
                return None

        # ── SPY trend gate ────────────────────────────────────────────────────────
        # Block long entries when the broad market is trending down over the last 15 min.
        # Gap-and-go setups are exempt — a stock gapping up vs a down SPY shows real RS.
        if not getattr(self, "_spy_trend_ok", True) and not _gap_go:
            self.database.record_decision(symbol, "SKIP", price,
                            reasoning="SPY trend gate: market trending down — no long entries",
                            signal_score=_ss, veto_rule="SPY_TREND_GATE")
            log.info("SPY trend gate %s: SPY bearish last 3 bars — skipping long entry", symbol)
            return None

        quote       = self.broker.get_latest_quote(symbol)
        spread_pct  = quote["spread_pct"] if quote else None
        limit_price = round(quote["ask"] + 0.01, 2) if quote else None

        if limit_price is None:
            self.database.record_decision(symbol, "SKIP", price,
                            reasoning="No valid quote — skipping to avoid unprotected market entry (IEX data gap or spread >5%)",
                            signal_score=_ss, veto_rule="NO_QUOTE")
            log.warning("Skip %s — no valid quote from IEX; refusing market-order fallback to avoid slippage", symbol)
            return None

        edgar_veto, edgar_reason = self.edgar.check_fresh_8k(symbol)
        if edgar_veto:
            self.database.record_decision(symbol, "SKIP", price,
                            reasoning=f"EDGAR 8-K gate: {edgar_reason}",
                            signal_score=_ss, veto_rule="EDGAR_8K")
            log.warning("EDGAR veto %s: %s", symbol, edgar_reason)
            return None

        ok, reason = self.risk_manager.approve_buy(
            symbol, price, qty, stop_loss,
            settled_cash, self._deployed_today, num_positions,
            effective_daily_pnl, equity, self._trades_today,
            rr, confidence, vol_ratio, rsi,
            spread_pct=spread_pct,
            key_levels=self._key_levels_cache.get(symbol),
            min_vol_ratio_override=_vol_floor,
        )
        if not ok:
            self.database.record_decision(symbol, "SKIP", price,
                            reasoning=f"Risk veto: {reason} | {full_reason}",
                            signal_score=_ss, veto_rule="RISK_MANAGER")
            log.info("BUY vetoed %s: %s", symbol, reason)
            return None

        funded_settled = settled_cash >= (price * qty)
        order = self.broker.place_bracket_order(
            symbol, qty, stop_loss, take_profit, limit_price=limit_price)
        if not order:
            return None

        order_id   = getattr(order, "id", None)
        fill_price = self.broker.get_fill_price(str(order_id), retries=6, delay=0.5) if order_id else None
        if fill_price is None:
            if order_id:
                cancel_failed = False
                try:
                    self.broker._trade_client.cancel_order_by_id(str(order_id))
                    log.warning("BUY %s: order %s not filled after 3s — cancelled", symbol, order_id)
                except Exception as _ce:
                    cancel_failed = True
                    log.warning("Could not cancel order %s for %s: %s — checking broker position", order_id, symbol, _ce)
                if cancel_failed:
                    # Cancel failed — likely filled during the race.  Re-check position.
                    try:
                        _broker_pos = self.broker.get_positions().get(symbol)
                        if _broker_pos is not None:
                            fill_price = float(getattr(_broker_pos, "avg_entry_price", None) or price)
                            log.info("BUY %s: cancel failed, broker position confirmed — fill=%.4f", symbol, fill_price)
                    except Exception as _pe:
                        log.warning("Broker position re-check failed for %s: %s", symbol, _pe)
            if fill_price is None:
                self.database.record_decision(symbol, "SKIP", price,
                                reasoning="Order submitted but fill not confirmed within 3s — cancelled",
                                signal_score=_ss, veto_rule="NO_FILL")
                return None

        slippage_per_sh  = fill_price - price
        slippage_dollars = slippage_per_sh * qty
        if abs(slippage_per_sh) > 0.01:
            log.info("Slippage %s: fill=%.4f decision=%.4f diff=%.4f total=$%.2f",
                     symbol, fill_price, price, slippage_per_sh, slippage_dollars)

        cost       = fill_price * qty
        setup_type = d.get("setup_type") or setup_type_hint or None
        with self._state_lock:
            self._deployed_today += cost
            self._trades_today   += 1
            self._traded_buckets_today.add(config.SYMBOL_BUCKET.get(symbol, "unknown"))
        self.gfv_tracker.record_buy(symbol, funded_by_settled=funded_settled)
        self.database.save_position(symbol, fill_price, qty, stop_loss, take_profit,
                      setup_type=setup_type)
        self.database.record_decision(symbol, "BUY", fill_price, qty, stop_loss, take_profit,
                        reasoning=full_reason, setup_type=setup_type, confidence=confidence,
                        slippage_dollars=round(slippage_dollars, 4))
        self.notifier.send_trade_alert(
            action="BUY", symbol=symbol, price=fill_price, qty=qty,
            equity=equity, daily_pnl=effective_daily_pnl,
            deployed=self._deployed_today, positions_open=num_positions + 1,
            stop_loss=stop_loss, take_profit=take_profit,
            setup_type=setup_type, reason=reason_entry,
        )
        return cost

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
            log.warning("GFV block — cannot sell %s: %s", symbol, gfv_reason)
            return None

        pos_data      = next((p for p in positions_snapshot if p["symbol"] == symbol), {})
        current_price = pos_data.get("current_price") or d.get("price") or 0
        total_qty     = float(pos_data.get("qty", 0))
        entry_price   = pos_data.get("entry_price", 0)

        if action == "PARTIAL_SELL":
            if total_qty < 2:
                log.info("Partial sell skipped for %s — only %.0f share(s), cannot split", symbol, total_qty)
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
            # Resubmit broker protection for the runner — cancel stale bracket legs
            # (sized for old qty) then place a fresh bracket for the remaining shares.
            if runner_qty >= 1 and runner_stop and runner_tp:
                try:
                    self.broker.cancel_orders_for_symbol(symbol)
                    self.broker.place_bracket_order(
                        symbol, int(runner_qty), runner_stop, runner_tp)
                    log.info("Runner bracket resubmitted %s: qty=%.0f SL=%.2f TP=%.2f",
                             symbol, runner_qty, runner_stop, runner_tp)
                except Exception as _re:
                    log.warning("Could not resubmit runner bracket for %s: %s — submitting stop only", symbol, _re)
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
        log.info("[DRY-RUN] Algo returned %d decisions — no orders will be placed:", len(decisions))
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
                     f"{ep:.2f}" if ep else "—",
                     f"{sl:.2f}" if sl else "—",
                     f"{tp:.2f}" if tp else "—",
                     qty or "—", conf or "—", rr or "—", risk or "—")
            if reason:
                log.info("         → %s", reason)

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
