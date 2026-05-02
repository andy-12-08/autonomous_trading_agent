from datetime import datetime
import config
from core.database import log


class ExecutorMixin:
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
        """Apply risk checks and submit broker orders for AI decisions (no-op if dry_run).

        Args:
            decisions: Parsed AI output rows.
            positions_snapshot: From build_positions_snapshot.
            settled_cash: T+1 settled buying power.
            equity: Account equity.
            effective_daily_pnl: Realized + unrealized for drawdown guard.
            dynamic_confidence_bar: Minimum confidence for new entries.
            vix_factor: Regime size multiplier.
            kelly_factor: Kelly sizing multiplier.
            cooling_symbols: Symbol -> skip reason.
            suppressed_setups: setup_type -> skip reason.
            signal_score_lookup: Symbol -> programmatic score.
            sector_strength: Sector -> strength string for bucket logic.

        Returns:
            None.
        """
        if dynamic_confidence_bar <= 0:
            dynamic_confidence_bar = config.MIN_SIGNAL_CONFIDENCE

        if self._dry_run:
            log.info("[DRY-RUN] AI returned %d decisions — no orders will be placed:", len(decisions))
            for d in decisions:
                sym    = (d.get("symbol") or "?").upper()
                action = (d.get("action") or "SKIP").upper()
                final  = (d.get("final_decision") or "SKIP").upper()
                ep     = d.get("entry_price")
                sl     = d.get("stop_loss")
                tp     = d.get("take_profit")
                qty    = d.get("qty")
                conf   = d.get("signal_confidence")
                rr     = d.get("reward_to_risk")
                risk   = d.get("risk_per_trade_dollars")
                reason = str(d.get("reason_for_entry") or "")[:90]
                log.info("  [%s] %-6s  action=%-12s  entry=%-8s  SL=%-8s  TP=%-8s  "
                         "qty=%-5s  conf=%s  R:R=%s  risk=$%s",
                         final, sym, action,
                         f"{ep:.2f}" if ep else "—",
                         f"{sl:.2f}" if sl else "—",
                         f"{tp:.2f}" if tp else "—",
                         qty or "—", conf or "—", rr or "—", risk or "—")
                if reason:
                    log.info("         → %s", reason)
            return

        open_symbols  = {p["symbol"] for p in positions_snapshot}
        num_positions = len(open_symbols)

        _score_lookup = signal_score_lookup or {}

        for d in decisions:
            symbol = (d.get("symbol") or "").upper()
            action = (d.get("action") or "SKIP").upper()
            final  = (d.get("final_decision") or "SKIP").upper()

            # Signal score from pre-AI filter — stored with every SKIP for lookback analysis
            _ss = _score_lookup.get(symbol)

            reason_entry = d.get("reason_for_entry") or d.get("reasoning") or ""
            reason_avoid = d.get("reason_to_avoid")  or ""
            full_reason  = reason_entry + (f" | AVOID: {reason_avoid}" if reason_avoid else "")

            # Respect AI's own SKIP / HOLD verdict
            if final == "SKIP" or action in ("SKIP", "HOLD"):
                self.database.record_decision(symbol, action,
                                d.get("entry_price") or d.get("price"),
                                reasoning=full_reason, signal_score=_ss,
                                veto_rule="AI_SKIP")
                continue

            # ── BUY ──────────────────────────────────────────────────────────
            if action == "BUY":
                if symbol in open_symbols:
                    log.info("Skip BUY %s — already holding", symbol)
                    continue

                # Use effective PnL (realized + unrealized) so open losers count
                if effective_daily_pnl <= -config.DAILY_DRAWDOWN_LIMIT:
                    self.database.record_decision(symbol, "SKIP", d.get("entry_price"),
                                    reasoning=f"Daily drawdown limit (${effective_daily_pnl:.0f} realized+unrealized). Rule 6.",
                                    signal_score=_ss, veto_rule="DRAWDOWN_LIMIT")
                    log.warning("Daily drawdown guard — no new buys. effective_pnl=%.0f", effective_daily_pnl)
                    continue

                # Plan posture check
                posture = (self._daily_plan or {}).get("risk_posture", "normal")
                if posture == "stand_aside":
                    self.database.record_decision(symbol, "SKIP", d.get("entry_price"),
                                    reasoning="Morning study: stand_aside — no new entries today",
                                    signal_score=_ss, veto_rule="STAND_ASIDE")
                    continue

                # Setup-type suppression (Rule 19: negative expectancy → block)
                setup_type_hint = d.get("setup_type") or d.get("setup_type_hint") or ""
                if suppressed_setups and setup_type_hint and setup_type_hint in suppressed_setups:
                    self.database.record_decision(symbol, "SKIP", d.get("entry_price"),
                                    reasoning=suppressed_setups[setup_type_hint],
                                    signal_score=_ss, veto_rule="SETUP_SUPPRESSED")
                    log.info("SETUP SUPPRESSED %s [%s]: %s",
                             symbol, setup_type_hint, suppressed_setups[setup_type_hint][:80])
                    continue

                # Symbol cooling-off (persistent per-symbol losing streak)
                if cooling_symbols and symbol in cooling_symbols:
                    self.database.record_decision(symbol, "SKIP", d.get("entry_price"),
                                    reasoning=cooling_symbols[symbol],
                                    signal_score=_ss, veto_rule="COOLING")
                    log.info("COOLING veto %s: %s", symbol, cooling_symbols[symbol])
                    continue

                # Earnings blackout (binary gap risk)
                eb_blocked, eb_reason = self.market_guard.is_earnings_blackout(symbol)
                if eb_blocked:
                    self.database.record_decision(symbol, "SKIP", d.get("entry_price"),
                                    reasoning=eb_reason,
                                    signal_score=_ss, veto_rule="EARNINGS_BLACKOUT")
                    continue

                # Revenge-trade guard + dynamic confidence
                confidence = d.get("signal_confidence", 0)
                consec     = self.expectancy_engine.get_recent_consecutive_losses(self.database.get_recent_decisions(40))
                rtg_ok, rtg_reason = self.expectancy_engine.check_revenge_trade_guard(consec, confidence)
                if not rtg_ok:
                    self.database.record_decision(symbol, "SKIP", d.get("entry_price"),
                                    reasoning=f"Revenge-trade guard: {rtg_reason}",
                                    signal_score=_ss, veto_rule="REVENGE_TRADE")
                    log.warning("REVENGE-TRADE guard %s: %s", symbol, rtg_reason)
                    continue

                # Dynamic confidence bar (raised when recent win rate < 40%)
                if confidence < dynamic_confidence_bar:
                    self.database.record_decision(symbol, "SKIP", d.get("entry_price"),
                                    reasoning=(f"Dynamic confidence bar: need {dynamic_confidence_bar}/10 "
                                               f"(recent form weak), got {confidence}/10"),
                                    signal_score=_ss, veto_rule="DYN_CONFIDENCE")
                    log.info("Dynamic bar blocked %s: conf=%d < bar=%d",
                             symbol, confidence, dynamic_confidence_bar)
                    continue

                price = d.get("entry_price") or d.get("price") or self.broker.get_latest_price(symbol)
                if not price:
                    continue

                stop_loss   = d.get("stop_loss")
                take_profit = d.get("take_profit")

                try:
                    df  = self.broker.get_bars(symbol, "5Min", days=2)
                    df  = self.indicators.compute_indicators(df)
                    atr = float(df["atr"].iloc[-1]) if not df.empty else price * 0.01
                    sig = self.indicators.get_signal_summary(df) if not df.empty else {}
                except Exception:
                    atr = price * 0.01
                    sig = {}

                # Skip if volatility is extreme (Rule 5: size ∝ 1/vol; extreme → skip)
                if self.risk_manager.is_too_volatile(atr, price):
                    self.database.record_decision(symbol, "SKIP", price,
                                    reasoning=f"ATR too high ({atr/price:.1%}) — skip (Rule 5)",
                                    signal_score=_ss, veto_rule="ATR_TOO_HIGH")
                    log.info("ATR too high for %s — skipping", symbol)
                    continue

                # Risk manager SL/TP is always authoritative — Claude doesn't have key-level
                # context precise enough to set stops reliably. Use Claude's only as fallback.
                rm_sl, rm_tp = self.risk_manager.compute_stop_take_profit(
                    price, atr, key_levels=self._key_levels_cache.get(symbol))
                if rm_sl and rm_tp:
                    if stop_loss and take_profit:
                        log.debug("Overriding Claude SL=%.2f/TP=%.2f with rm SL=%.2f/TP=%.2f",
                                  stop_loss, take_profit, rm_sl, rm_tp)
                    stop_loss, take_profit = rm_sl, rm_tp
                elif not stop_loss or not take_profit:
                    stop_loss = float(rm_sl or stop_loss or price * 0.98)
                    take_profit = float(rm_tp or take_profit or price * 1.04)
                if not stop_loss or not take_profit:
                    self.database.record_decision(symbol, "SKIP", price,
                                    reasoning="Could not compute valid SL/TP — skipping",
                                    signal_score=_ss, veto_rule="NO_LEVELS")
                    continue

                # Bucket diversification check
                bucket_ok, bucket_reason = self.bucket_manager.bucket_is_open(
                    symbol, positions_snapshot, confidence, sector_strength=sector_strength
                )
                if not bucket_ok:
                    self.database.record_decision(symbol, "SKIP", price,
                                    reasoning=f"Bucket veto: {bucket_reason}",
                                    signal_score=_ss, veto_rule="BUCKET")
                    log.info("BUCKET veto %s: %s", symbol, bucket_reason)
                    continue

                # Correlation guard (factor concentration risk)
                corr_ok, corr_reason = self.market_guard.check_correlation(
                    symbol, positions_snapshot)
                if not corr_ok:
                    self.database.record_decision(symbol, "SKIP", price, reasoning=corr_reason,
                                    signal_score=_ss, veto_rule="CORRELATION")
                    log.info("CORRELATION veto %s: %s", symbol, corr_reason)
                    continue

                # Volatility-adjusted, confidence-scaled, VIX + Kelly-aware sizing
                qty = self.risk_manager.calc_qty(price, stop_loss, settled_cash, self._deployed_today,
                                  equity, atr=atr, confidence=confidence,
                                  vix_factor=vix_factor, kelly_factor=kelly_factor)
                if qty <= 0:
                    self.database.record_decision(symbol, "SKIP", price,
                                    reasoning="qty=0 after vol-adjusted sizing",
                                    signal_score=_ss, veto_rule="QTY_ZERO")
                    continue

                # Portfolio heat check (simultaneous stop-out risk)
                new_risk = (price - stop_loss) * qty if stop_loss else 0
                heat_ok, heat_reason = self.risk_manager.check_portfolio_heat(
                    positions_snapshot, new_risk, equity)
                if not heat_ok:
                    self.database.record_decision(symbol, "SKIP", price, reasoning=heat_reason,
                                    signal_score=_ss, veto_rule="PORTFOLIO_HEAT")
                    log.warning("PORTFOLIO HEAT veto %s: %s", symbol, heat_reason)
                    continue

                rr        = float(d.get("reward_to_risk") or 0.0)
                vol_ratio = float(sig.get("vol_ratio") or d.get("vol_ratio") or 0.0)
                rsi       = float(sig.get("rsi")       or d.get("rsi")       or 50.0)

                # Early-window vol_ratio relaxation (Option A + B)
                # Option A: all stocks in 9:35–10:30 ET use a lower floor (0.6).
                # Option B: gap-and-go stocks (gap >= 2%, holding VWAP) use an even
                #           lower floor (0.5) because the gap itself is the volume event.
                _now_et      = datetime.now(self._ET)
                _cur_min     = _now_et.hour * 60 + _now_et.minute
                _open_min    = config.MARKET_OPEN_HOUR * 60 + config.MARKET_OPEN_MIN   # 570
                _early_end   = config.EARLY_WINDOW_END_HOUR * 60 + config.EARLY_WINDOW_END_MIN  # 630
                _in_early    = _open_min <= _cur_min < _early_end
                _gap_pct     = float(sig.get("gap_pct", 0))
                _above_vwap  = bool(sig.get("above_vwap", False))
                _gap_go      = _in_early and _gap_pct >= config.GAP_AND_GO_MIN_VOL_PCT and _above_vwap

                if _gap_go:
                    _vol_floor = config.GAP_AND_GO_VOL_RATIO      # 0.5 — Option B
                elif _in_early:
                    _vol_floor = config.EARLY_WINDOW_VOL_RATIO    # 0.6 — Option A
                else:
                    _vol_floor = None                              # use config default (0.7)

                if _gap_go:
                    log.info("Gap-and-go early entry %s: gap=%.1f%% above_vwap=True — vol floor relaxed to %.1f",
                             symbol, _gap_pct, config.GAP_AND_GO_VOL_RATIO)
                elif _in_early:
                    log.info("Early window %s: vol floor relaxed to %.1f (was %.1f)",
                             symbol, config.EARLY_WINDOW_VOL_RATIO, config.MIN_VOL_RATIO_ENTRY)

                # Rule 1: check bid-ask spread; also extract limit_price for limit entry
                quote       = self.broker.get_latest_quote(symbol)
                spread_pct  = quote["spread_pct"] if quote else None
                # Limit entry = ask + $0.01 to ensure fill while avoiding market-order slippage
                limit_price = round(quote["ask"] + 0.01, 2) if quote else None

                # SEC EDGAR 8-K gate — veto if material event filed today
                edgar_veto, edgar_reason = self.edgar.check_fresh_8k(symbol)
                if edgar_veto:
                    self.database.record_decision(symbol, "SKIP", price,
                                    reasoning=f"EDGAR 8-K gate: {edgar_reason}",
                                    signal_score=_ss, veto_rule="EDGAR_8K")
                    log.warning("EDGAR veto %s: %s", symbol, edgar_reason)
                    continue

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
                    continue

                # Determine if funded by settled cash (GFV safety)
                funded_settled = settled_cash >= (price * qty)
                order = self.broker.place_bracket_order(
                    symbol, qty, stop_loss, take_profit, limit_price=limit_price)
                if order:
                    # Use actual fill price from Alpaca — market orders fill at ask,
                    # which may differ slightly from Claude's suggested entry price.
                    order_id         = getattr(order, "id", None)
                    fill_price       = (self.broker.get_fill_price(str(order_id)) if order_id else None) or price
                    slippage_per_sh  = fill_price - price
                    slippage_dollars = slippage_per_sh * qty
                    if abs(slippage_per_sh) > 0.01:
                        log.info("Slippage %s: fill=%.4f decision=%.4f diff=%.4f total=$%.2f",
                                 symbol, fill_price, price, slippage_per_sh, slippage_dollars)
                    cost       = fill_price * qty
                    setup_type = d.get("setup_type") or setup_type_hint or None
                    with self._state_lock:
                        self._deployed_today    += cost
                        self._trades_today      += 1
                    settled_cash                -= cost
                    num_positions               += 1
                    open_symbols.add(symbol)
                    self._traded_buckets_today.add(config.SYMBOL_BUCKET.get(symbol, "unknown"))
                    self.database.save_position(symbol, fill_price, qty, stop_loss, take_profit,
                                  setup_type=setup_type)
                    self.gfv_tracker.record_buy(symbol, funded_by_settled=funded_settled)
                    self.database.record_decision(symbol, "BUY", fill_price, qty, stop_loss, take_profit,
                                    reasoning=full_reason, setup_type=setup_type,
                                    confidence=confidence,
                                    slippage_dollars=round(slippage_dollars, 4))
                    self.notifier.send_trade_alert(
                        action="BUY", symbol=symbol, price=fill_price, qty=qty,
                        equity=equity, daily_pnl=effective_daily_pnl,
                        deployed=self._deployed_today, positions_open=num_positions,
                        stop_loss=stop_loss, take_profit=take_profit,
                        setup_type=setup_type, reason=reason_entry,
                    )

            # ── SELL / PARTIAL_SELL ───────────────────────────────────────────
            elif action in ("SELL", "PARTIAL_SELL"):
                if symbol not in open_symbols:
                    continue

                # GFV safety check before selling
                gfv_safe, gfv_reason = self.gfv_tracker.gfv_safe_to_sell(symbol)
                if not gfv_safe:
                    self.database.record_decision(symbol, "SKIP", None,
                                    reasoning=f"GFV block: {gfv_reason}",
                                    veto_rule="GFV_LOCK")
                    log.warning("GFV block — cannot sell %s: %s", symbol, gfv_reason)
                    continue

                pos_data      = next((p for p in positions_snapshot if p["symbol"] == symbol), {})
                current_price = pos_data.get("current_price") or d.get("price") or 0
                total_qty     = float(pos_data.get("qty", 0))
                entry_price   = pos_data.get("entry_price", 0)

                if action == "PARTIAL_SELL":
                    qty = max(1, int(total_qty // 2))
                    # P&L for the shares actually sold, not full-position unrealized
                    pnl = (current_price - entry_price) * qty if entry_price else 0
                else:
                    qty = total_qty
                    pnl = pos_data.get("pnl", 0.0)

                order = self.broker.place_market_order(symbol, qty, "SELL")
                if order:
                    with self._state_lock:
                        self._daily_pnl += pnl
                    setup_type  = pos_data.get("setup_type") or d.get("setup_type")
                    self.database.record_decision(symbol, action, current_price, qty, pnl=pnl,
                                    reasoning=full_reason, setup_type=setup_type)
                    if action == "PARTIAL_SELL":
                        self.database.save_position(symbol, pos_data.get("entry_price", 0),
                                      float(pos_data.get("qty", 0)) - qty,
                                      pos_data.get("stop_loss", 0),
                                      pos_data.get("take_profit", 0),
                                      trailing=pos_data.get("trailing", False),
                                      highest_price=pos_data.get("current_price"),
                                      partial_taken=True,
                                      entry_ts=pos_data.get("entry_ts", ""))
                    if action == "SELL":
                        self.database.remove_position(symbol)
                        self.gfv_tracker.remove_buy(symbol)
                        open_symbols.discard(symbol)
                        num_positions -= 1
                        outcome = "win" if pnl > 0 else "loss" if pnl < 0 else "breakeven"
                        self.database.update_outcome(symbol, outcome, pnl)
                    self.notifier.send_trade_alert(
                        action=action, symbol=symbol, price=current_price, qty=qty,
                        equity=equity, daily_pnl=self._daily_pnl,
                        deployed=self._deployed_today, positions_open=num_positions,
                        pnl=pnl, setup_type=setup_type, reason=full_reason,
                    )

            # ── UPDATE_STOP ───────────────────────────────────────────────────
            elif action == "UPDATE_STOP":
                new_stop = d.get("stop_loss")
                if new_stop and symbol in open_symbols:
                    pos_data     = next((p for p in positions_snapshot if p["symbol"] == symbol), {})
                    current_stop = pos_data.get("stop_loss", 0)
                    ok, reason   = self.risk_manager.approve_stop_update(symbol, new_stop, current_stop)
                    if not ok:
                        log.info("UPDATE_STOP rejected %s: %s", symbol, reason)
                        continue
                    self.broker.update_stop_loss(symbol, new_stop)
                    self.database.save_position(symbol,
                                  pos_data.get("entry_price", 0),
                                  pos_data.get("qty", 0),
                                  new_stop,
                                  pos_data.get("take_profit", 0))
                    self.database.record_decision(symbol, "UPDATE_STOP",
                                    pos_data.get("current_price") or d.get("price"),
                                    stop_loss=new_stop, reasoning=full_reason)

