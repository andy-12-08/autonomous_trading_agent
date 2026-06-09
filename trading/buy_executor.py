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


class BuyExecutorMixin:
    """BUY execution workflow: preflight gates, sizing, order fills, and persistence."""

    def _record_buy_skip(
        self,
        symbol: str,
        price: float | None,
        reasoning: str,
        signal_score,
        veto_rule: str,
    ) -> None:
        """Persist a skipped BUY decision with consistent audit fields.

        Args:
            symbol: Ticker that was rejected.
            price: Reference price, if known.
            reasoning: Human-readable veto reason.
            signal_score: Programmatic signal score to store for review.
            veto_rule: Machine-readable veto rule label.

        Returns:
            None.
        """
        self.database.record_decision(
            symbol,
            "SKIP",
            price,
            reasoning=reasoning,
            signal_score=signal_score,
            veto_rule=veto_rule,
        )

    def _preflight_buy(
        self,
        d: dict,
        symbol: str,
        signal_score,
        open_symbols: set,
        effective_daily_pnl: float,
        dynamic_confidence_bar: int,
        cooling_symbols: dict,
        suppressed_setups: dict,
    ) -> dict | None:
        """Run stateful vetoes that do not require fresh quote or sizing data.

        Args:
            d: Raw decision dict.
            symbol: Uppercase ticker.
            signal_score: Scanner score used for audit logging.
            open_symbols: Symbols already held.
            effective_daily_pnl: Realized plus unrealized P&L for drawdown checks.
            dynamic_confidence_bar: Current minimum confidence threshold.
            cooling_symbols: Symbol-level cooling-off reasons.
            suppressed_setups: Setup-level suppression reasons.

        Returns:
            Dict with confidence and setup_type_hint when preflight passes, otherwise None.
        """
        if symbol in open_symbols:
            log.info("Skip BUY %s - already holding", symbol)
            return None

        if effective_daily_pnl <= -config.DAILY_DRAWDOWN_LIMIT:
            self._record_buy_skip(
                symbol,
                d.get("entry_price"),
                f"Daily drawdown limit (${effective_daily_pnl:.0f} realized+unrealized). Rule 6.",
                signal_score,
                "DRAWDOWN_LIMIT",
            )
            log.warning("Daily drawdown guard - no new buys. effective_pnl=%.0f", effective_daily_pnl)
            return None

        today = datetime.now(self._ET).date().isoformat()
        recent = self.database.get_recent_decisions(300)
        today_closes = [
            row for row in recent
            if (row.get("ts") or "").startswith(today)
            and row.get("action") in ("SELL", "PARTIAL_SELL")
            and row.get("pnl") is not None
        ]
        daily_losses = sum(1 for row in today_closes if (row.get("pnl") or 0) < 0)
        symbol_lost_today = any(
            row.get("symbol") == symbol and (row.get("pnl") or 0) < 0
            for row in today_closes
        )
        if symbol_lost_today:
            self._record_buy_skip(
                symbol,
                d.get("entry_price"),
                "Same-day symbol loss cooldown: do not re-enter a ticker after it already failed today",
                signal_score,
                "SYMBOL_LOSS_COOLDOWN",
            )
            log.info("SYMBOL LOSS cooldown %s: previous same-day loss", symbol)
            return None
        if daily_losses >= config.MAX_DAILY_LOSING_TRADES:
            self._record_buy_skip(
                symbol,
                d.get("entry_price"),
                f"Daily loss stop: {daily_losses} losing trades today; standing aside",
                signal_score,
                "DAILY_LOSS_STOP",
            )
            log.warning("Daily loss stop - no new buys after %d losses", daily_losses)
            return None
        if daily_losses >= config.DAILY_LOSS_TIGHTEN_AFTER:
            confidence_hint = int(float(d.get("signal_confidence") or 0))
            score_hint = float(signal_score or 0)
            if score_hint < config.POST_LOSS_MIN_SCORE or confidence_hint < config.POST_LOSS_MIN_CONF:
                self._record_buy_skip(
                    symbol,
                    d.get("entry_price"),
                    (
                        f"Post-loss throttle: after {daily_losses} loss(es), need "
                        f"score>={config.POST_LOSS_MIN_SCORE} and conf>={config.POST_LOSS_MIN_CONF}; "
                        f"got score={score_hint:.1f}, conf={confidence_hint}"
                    ),
                    signal_score,
                    "POST_LOSS_THROTTLE",
                )
                log.info("POST LOSS throttle %s: score=%.1f conf=%d", symbol, score_hint, confidence_hint)
                return None

        setup_type_hint = d.get("setup_type") or d.get("setup_type_hint") or ""
        _suppression_key = setup_type_hint or "momentum"
        if suppressed_setups and _suppression_key in suppressed_setups:
            self._record_buy_skip(
                symbol,
                d.get("entry_price"),
                suppressed_setups[_suppression_key],
                signal_score,
                "SETUP_SUPPRESSED",
            )
            log.info(
                "SETUP SUPPRESSED %s [%s]: %s",
                symbol,
                _suppression_key,
                suppressed_setups[_suppression_key][:80],
            )
            return None

        if cooling_symbols and symbol in cooling_symbols:
            self._record_buy_skip(
                symbol,
                d.get("entry_price"),
                cooling_symbols[symbol],
                signal_score,
                "COOLING",
            )
            log.info("COOLING veto %s: %s", symbol, cooling_symbols[symbol])
            return None

        eb_blocked, eb_reason = self.market_guard.is_earnings_blackout(symbol)
        if eb_blocked:
            self._record_buy_skip(symbol, d.get("entry_price"), eb_reason, signal_score, "EARNINGS_BLACKOUT")
            return None

        confidence = int(float(d.get("signal_confidence") or 0))
        consec = self.expectancy_engine.get_recent_consecutive_losses(
            self.database.get_recent_decisions(40)
        )
        rtg_ok, rtg_reason = self.expectancy_engine.check_revenge_trade_guard(consec, confidence)
        if not rtg_ok:
            self._record_buy_skip(
                symbol,
                d.get("entry_price"),
                f"Revenge-trade guard: {rtg_reason}",
                signal_score,
                "REVENGE_TRADE",
            )
            log.warning("REVENGE-TRADE guard %s: %s", symbol, rtg_reason)
            return None

        if confidence < dynamic_confidence_bar:
            self._record_buy_skip(
                symbol,
                d.get("entry_price"),
                (
                    f"Dynamic confidence bar: need {dynamic_confidence_bar}/10 "
                    f"(recent form weak), got {confidence}/10"
                ),
                signal_score,
                "DYN_CONFIDENCE",
            )
            log.info("Dynamic bar blocked %s: conf=%d < bar=%d", symbol, confidence, dynamic_confidence_bar)
            return None

        return {"confidence": confidence, "setup_type_hint": setup_type_hint}

    def _load_buy_market_context(
        self,
        d: dict,
        symbol: str,
        signal_score,
    ) -> dict | None:
        """Resolve entry price, indicators, ATR, stop loss, and bracket take profit.

        Args:
            d: Raw decision dict with optional entry and level hints.
            symbol: Uppercase ticker.
            signal_score: Scanner score used for audit logging.

        Returns:
            Dict with price, atr, sig, stop_loss, and take_profit, or None when invalid.
        """
        price = d.get("entry_price") or d.get("price") or self.broker.get_latest_price(symbol)
        if not price:
            return None

        stop_loss = d.get("stop_loss")
        take_profit = d.get("take_profit")

        try:
            df = self.broker.get_bars(symbol, "5Min", days=2)
            df = self.indicators.compute_indicators(df)
            atr = float(df["atr"].iloc[-1]) if not df.empty else price * 0.01
            sig = self.indicators.get_signal_summary(df) if not df.empty else {}
            if not df.empty and len(df) >= 3 and sig:
                last = df.iloc[-1]
                prev = df.iloc[-2]
                recent = df.tail(3)
                recent_low = float(recent["low"].min())
                ema9 = float(sig.get("ema9") or 0)
                ema21 = float(sig.get("ema21") or 0)
                vwap = float(sig.get("vwap") or 0)
                support_levels = [v for v in (ema9, ema21, vwap) if v > 0]
                nearest_support = max([v for v in support_levels if v <= price] or support_levels or [0])
                touch_line = nearest_support * (1 + config.PULLBACK_TOUCH_TOLERANCE_PCT) if nearest_support else 0
                sig.update({
                    "last_high": round(float(last["high"]), 4),
                    "last_low": round(float(last["low"]), 4),
                    "prev_high": round(float(prev["high"]), 4),
                    "recent_low_3": round(recent_low, 4),
                    "close_above_prev_high": bool(float(last["close"]) > float(prev["high"])),
                    "nearest_entry_support": round(nearest_support, 4) if nearest_support else 0.0,
                    "pullback_touched_support": bool(touch_line and recent_low <= touch_line),
                    "entry_distance_from_support_pct": round(
                        (price - nearest_support) / nearest_support if nearest_support else 0.0, 5
                    ),
                })
        except Exception as exc:
            log.warning("Could not fetch bars/indicators for %s: %s - using ATR fallback", symbol, exc)
            atr = price * 0.01
            sig = {}

        if self.risk_manager.is_too_volatile(atr, price):
            self._record_buy_skip(
                symbol,
                price,
                f"ATR too high ({atr / price:.1%}) - skip (Rule 5)",
                signal_score,
                "ATR_TOO_HIGH",
            )
            log.info("ATR too high for %s - skipping", symbol)
            return None

        rm_sl, rm_tp = self.risk_manager.compute_stop_take_profit(
            price, atr, key_levels=self._key_levels_cache.get(symbol)
        )
        if rm_sl and rm_tp:
            if stop_loss and take_profit:
                log.debug(
                    "Overriding decision SL=%.2f/TP=%.2f with risk-manager SL=%.2f/TP=%.2f",
                    stop_loss,
                    take_profit,
                    rm_sl,
                    rm_tp,
                )
            stop_loss, take_profit = rm_sl, rm_tp
        elif not stop_loss or not take_profit:
            stop_loss = float(rm_sl or stop_loss or price * 0.98)
            take_profit = float(rm_tp or take_profit or price * 1.04)

        take_profit = round(price * config.BRACKET_TP_SAFETY, 2)
        if not stop_loss or not take_profit:
            self._record_buy_skip(
                symbol,
                price,
                "Could not compute valid SL/TP - skipping",
                signal_score,
                "NO_LEVELS",
            )
            return None

        return {
            "price": price,
            "atr": atr,
            "sig": sig,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
        }

    def _size_buy_order(
        self,
        symbol: str,
        price: float,
        stop_loss: float,
        atr: float,
        confidence: int,
        settled_cash: float,
        equity: float,
        vix_factor: float,
        kelly_factor: float,
        positions_snapshot: list[dict],
        sector_strength: dict | None,
        score_lookup: dict,
        signal_score,
    ) -> float | None:
        """Run portfolio gates and calculate the share quantity for a BUY.

        Args:
            symbol: Uppercase ticker.
            price: Proposed entry price.
            stop_loss: Protective stop price.
            atr: Average true range used for volatility sizing.
            confidence: Signal confidence integer.
            settled_cash: Available settled buying power.
            equity: Account equity.
            vix_factor: Market-regime sizing multiplier.
            kelly_factor: Historical-edge sizing multiplier.
            positions_snapshot: Open positions for bucket and heat checks.
            sector_strength: Sector strength map for bucket logic.
            score_lookup: Symbol-to-score map.
            signal_score: Current symbol score used for audit logging.

        Returns:
            Whole-share quantity as a float, or None when a gate vetoes the trade.
        """
        bucket_ok, bucket_reason = self.bucket_manager.bucket_is_open(
            symbol, positions_snapshot, confidence, sector_strength=sector_strength
        )
        if not bucket_ok:
            self._record_buy_skip(symbol, price, f"Bucket veto: {bucket_reason}", signal_score, "BUCKET")
            log.info("BUCKET veto %s: %s", symbol, bucket_reason)
            return None

        sym_score = float(score_lookup.get(symbol) or 0.0)
        conviction_cap = _conviction_cap(sym_score, self._deployed_today)
        if conviction_cap <= 0:
            self._record_buy_skip(
                symbol,
                price,
                "Daily capital exhausted - conviction cap below minimum",
                signal_score,
                "QTY_ZERO",
            )
            log.info("Daily capital exhausted for %s - skipping", symbol)
            return None

        # Macro-day half-sizing: FOMC / CPI / NFP days carry whipsaw risk that
        # overwhelms technical setups.  Still trade, but at half normal size so
        # a single stop-out can't do serious damage.
        _warnings = (self._daily_plan or {}).get("special_warnings") or []
        _macro_keywords = ("FOMC", "CPI", "NFP", "GDP", "PCE", "JOLTS", "PPI")
        if any(kw in str(w) for kw in _macro_keywords for w in _warnings):
            conviction_cap = round(conviction_cap * config.MACRO_WARNING_SIZE_FACTOR, 2)
            log.warning(
                "Macro-day half-size %s: cap reduced to $%.0f (%.0f%% factor)",
                symbol, conviction_cap, config.MACRO_WARNING_SIZE_FACTOR * 100,
            )

        log.info(
            "Conviction cap %s: score=%.1f -> $%.0f (%.0f%% of $%.0f daily cap)",
            symbol,
            sym_score,
            conviction_cap,
            conviction_cap / config.MAX_DAILY_CAPITAL * 100,
            config.MAX_DAILY_CAPITAL,
        )
        qty = self.risk_manager.calc_qty(
            price,
            stop_loss,
            settled_cash,
            self._deployed_today,
            equity,
            atr=atr,
            confidence=confidence,
            vix_factor=vix_factor,
            kelly_factor=kelly_factor,
            position_cap=conviction_cap,
        )
        if qty <= 0:
            self._record_buy_skip(symbol, price, "qty=0 after vol-adjusted sizing", signal_score, "QTY_ZERO")
            return None

        if config.MIN_POSITION_SIZE > 0 and qty * price < config.MIN_POSITION_SIZE:
            self._record_buy_skip(
                symbol, price,
                f"Micro-position: ${qty * price:.0f} < ${config.MIN_POSITION_SIZE:.0f} minimum  "
                f"trade slot wasted on {qty:.0f} share(s) — skipping",
                signal_score, "QTY_ZERO",
            )
            log.info("MIN POSITION SIZE veto %s: $%.0f < $%.0f floor (%g shares)",
                     symbol, qty * price, config.MIN_POSITION_SIZE, qty)
            return None

        new_risk = (price - stop_loss) * qty if stop_loss else 0
        heat_ok, heat_reason = self.risk_manager.check_portfolio_heat(
            positions_snapshot, new_risk, equity
        )
        if not heat_ok:
            self._record_buy_skip(symbol, price, heat_reason, signal_score, "PORTFOLIO_HEAT")
            log.warning("PORTFOLIO HEAT veto %s: %s", symbol, heat_reason)
            return None

        return qty

    def _build_entry_gate_context(self, symbol: str, sig: dict) -> dict:
        """Compute time-window and setup flags used by late-day and SPY gates.

        Args:
            symbol: Uppercase ticker being evaluated.
            sig: Indicator summary dict for the symbol.

        Returns:
            Dict containing cur_min, gap_go, vwap_reclaim, and vol_floor.
        """
        now_et = datetime.now(self._ET)
        cur_min = now_et.hour * 60 + now_et.minute
        open_min = config.MARKET_OPEN_HOUR * 60 + config.MARKET_OPEN_MIN
        early_end = config.EARLY_WINDOW_END_HOUR * 60 + config.EARLY_WINDOW_END_MIN
        in_early = open_min <= cur_min < early_end
        gap_pct = float(sig.get("gap_pct", 0))
        gap_go = in_early and gap_pct >= config.GAP_AND_GO_MIN_VOL_PCT and bool(sig.get("above_vwap"))
        vwap_reclaim = sig.get("setup") == "vwap_reclaim"

        if gap_go:
            vol_floor = config.GAP_AND_GO_VOL_RATIO
            log.info(
                "Gap-and-go early entry %s: gap=%.1f%% above_vwap=True - vol floor relaxed to %.1f",
                symbol,
                gap_pct,
                config.GAP_AND_GO_VOL_RATIO,
            )
        elif in_early:
            vol_floor = config.EARLY_WINDOW_VOL_RATIO
            log.info(
                "Early window %s: vol floor relaxed to %.1f (was %.1f)",
                symbol,
                config.EARLY_WINDOW_VOL_RATIO,
                config.MIN_VOL_RATIO_ENTRY,
            )
        else:
            vol_floor = None

        return {
            "cur_min": cur_min,
            "in_early": in_early,
            "gap_go": gap_go,
            "vwap_reclaim": vwap_reclaim,
            "vol_floor": vol_floor,
        }

    def _validate_market_entry(
        self,
        d: dict,
        symbol: str,
        price: float,
        qty: float,
        stop_loss: float,
        take_profit: float,
        sig: dict,
        settled_cash: float,
        equity: float,
        effective_daily_pnl: float,
        num_positions: int,
        confidence: int,
        signal_score,
        full_reason: str,
    ) -> float | None:
        """Run time, quote, filing, and risk-manager gates before order submission.

        Args:
            d: Raw decision dict.
            symbol: Uppercase ticker.
            price: Proposed entry price.
            qty: Calculated share quantity.
            stop_loss: Protective stop price.
            take_profit: Bracket take-profit placeholder.
            sig: Indicator summary dict.
            settled_cash: Available settled cash.
            equity: Account equity.
            effective_daily_pnl: Realized plus unrealized P&L.
            num_positions: Current open position count.
            confidence: Signal confidence integer.
            signal_score: Scanner score for audit logging.
            full_reason: Combined algo reason string.

        Returns:
            Limit entry price if all gates pass, otherwise None.
        """
        stop_dist = price - stop_loss if stop_loss else 0.0
        reward_dist = take_profit - price if take_profit else 0.0
        rr = round(reward_dist / stop_dist, 2) if stop_dist > 0 else 0.0
        vol_ratio = _safe_float(sig.get("rvol") or sig.get("vol_ratio") or d.get("vol_ratio"), 0.0)
        rsi = _safe_float(sig.get("rsi") or d.get("rsi"), 50.0)
        gate_ctx = self._build_entry_gate_context(symbol, sig)

        prime_end = config.PRIME_ENTRY_END_HOUR * 60 + config.PRIME_ENTRY_END_MIN
        midmorning_end = config.MIDMORNING_ENTRY_END_HOUR * 60 + config.MIDMORNING_ENTRY_END_MIN
        close_min = config.MARKET_CLOSE_HOUR * 60 + config.MARKET_CLOSE_MIN

        if gate_ctx["cur_min"] >= close_min:
            self._record_buy_skip(
                symbol,
                price,
                "Late-day gate: no new entries after 3:45 PM ET",
                signal_score,
                "LATE_DAY_GATE",
            )
            log.info("Late-day gate: no new entries after 3:45 ET - skip %s", symbol)
            return None

        minutes_to_close = close_min - gate_ctx["cur_min"]
        if minutes_to_close < config.MIN_ENTRY_RUNWAY_MINUTES:
            reason = (
                f"Runway gate: only {minutes_to_close} min before EOD close; "
                f"need >= {config.MIN_ENTRY_RUNWAY_MINUTES} min so breakeven/time-stop logic can work"
            )
            self._record_buy_skip(symbol, price, reason, signal_score, "RUNWAY_GATE")
            log.info("RUNWAY gate %s: %d min to EOD close - skipping", symbol, minutes_to_close)
            return None

        if gate_ctx["cur_min"] > prime_end:
            decision_conf = int(d.get("signal_confidence") or d.get("confidence") or 0)
            in_midmorning = gate_ctx["cur_min"] < midmorning_end
            in_power_hour = any(
                (sh * 60 + sm) <= gate_ctx["cur_min"] <= (eh * 60 + em)
                and sh >= 12
                for sh, sm, eh, em in config.HIGH_VOLUME_WINDOWS
            )
            if in_midmorning:
                min_score = config.MIDMORNING_ENTRY_MIN_SCORE
                min_conf = config.MIDMORNING_ENTRY_MIN_CONF
                gate_label = "mid-morning"
            elif in_power_hour:
                min_score = config.POWER_HOUR_ENTRY_MIN_SCORE
                min_conf = config.POWER_HOUR_ENTRY_MIN_CONF
                gate_label = "power-hour"
            else:
                min_score = config.MIDDAY_ENTRY_MIN_SCORE
                min_conf = config.MIDDAY_ENTRY_MIN_CONF
                gate_label = "midday"
            if signal_score < min_score or decision_conf < min_conf:
                self._record_buy_skip(
                    symbol,
                    price,
                    (
                        f"{gate_label.title()} gate: score {signal_score:.1f}<{min_score} "
                        f"or conf {decision_conf}<{min_conf} outside prime window"
                    ),
                    signal_score,
                    (
                        "MIDMORNING_GATE"
                        if in_midmorning else
                        "POWER_HOUR_GATE"
                        if in_power_hour else
                        "MIDDAY_GATE"
                    ),
                )
                log.info(
                    "%s gate %s: score=%.1f conf=%d - need >=%.1f/>=%d outside 9:30-10:15 prime window",
                    gate_label.title(),
                    symbol,
                    signal_score,
                    decision_conf,
                    min_score,
                    min_conf,
                )
                return None

        # Entry momentum gate: require the stock to be actively rising at entry.
        # Blocks entering stocks that are mid-drop — "buy and fall with it" trades.
        # Gap-and-go setups and VWAP reclaims are exempted (they have their own
        # confirmation via first-bar-high / vwap_cross_up).
        _last_bullish   = bool(sig.get("last_bar_bullish", True))
        _bars_rising    = int(sig.get("bars_rising_3", 3))
        _price_vs_3ago  = float(sig.get("price_vs_3bars_ago", 0.0))
        _vwap_reclaim   = bool(sig.get("vwap_cross_up", False))
        _gap_go         = gate_ctx.get("gap_go", False)
        _momentum_ok = _last_bullish or _bars_rising >= 2 or _price_vs_3ago > 0.05
        if not _momentum_ok and not _vwap_reclaim and not _gap_go:
            reason = (
                f"Momentum gate: last bar {'red' if not _last_bullish else 'green'}, "
                f"{_bars_rising}/3 rising bars, price {_price_vs_3ago:+.2f}% vs 15min ago  "
                f"stock falling at entry, wait for upward momentum"
            )
            self._record_buy_skip(symbol, price, reason, signal_score, "MOMENTUM_GATE")
            log.info("MOMENTUM gate %s: falling at entry (bars_rising=%d, 15m=%+.2f%%) - skipping",
                     symbol, _bars_rising, _price_vs_3ago)
            return None

        if config.REQUIRE_PULLBACK_RECLAIM and not _gap_go:
            _ema9 = float(sig.get("ema9") or 0)
            _ema21 = float(sig.get("ema21") or 0)
            _above_vwap = bool(sig.get("above_vwap", False))
            _trend_aligned = _above_vwap and _ema9 > _ema21 > 0
            _pullback_touched = bool(sig.get("pullback_touched_support", False))
            _continuation_break = bool(
                sig.get("close_above_prev_high")
                or sig.get("above_orb_30")
                or sig.get("vwap_cross_up")
            )
            _support_dist = float(sig.get("entry_distance_from_support_pct") or 0.0)
            _strict_momentum = (
                _last_bullish
                and _bars_rising >= config.MIN_RISING_BARS_FOR_CONTINUATION
                and _price_vs_3ago >= config.CONTINUATION_MIN_15M_PCT
            )
            if not _trend_aligned:
                reason = "Continuation gate: price must be above VWAP with EMA9>EMA21 before entry"
                self._record_buy_skip(symbol, price, reason, signal_score, "CONTINUATION_GATE")
                log.info("CONTINUATION gate %s: trend not aligned", symbol)
                return None
            if _support_dist > config.MAX_ENTRY_DISTANCE_FROM_SUPPORT_PCT:
                reason = (
                    f"Pullback gate: entry {_support_dist:.2%} above nearest support "
                    f"(max {config.MAX_ENTRY_DISTANCE_FROM_SUPPORT_PCT:.2%}); wait for cleaner pullback"
                )
                self._record_buy_skip(symbol, price, reason, signal_score, "PULLBACK_GATE")
                log.info("PULLBACK gate %s: %.2f%% from support", symbol, _support_dist * 100)
                return None
            if not (_pullback_touched or _vwap_reclaim):
                reason = "Pullback gate: no recent touch/reclaim of VWAP/EMA support before continuation"
                self._record_buy_skip(symbol, price, reason, signal_score, "PULLBACK_GATE")
                log.info("PULLBACK gate %s: no recent support touch", symbol)
                return None
            if not (_continuation_break and _strict_momentum):
                reason = (
                    f"Continuation gate: need breakout/reclaim plus active climb; "
                    f"break={_continuation_break}, last_green={_last_bullish}, "
                    f"bars={_bars_rising}/3, 15m={_price_vs_3ago:+.2f}%"
                )
                self._record_buy_skip(symbol, price, reason, signal_score, "CONTINUATION_GATE")
                log.info("CONTINUATION gate %s: break=%s bars=%d 15m=%+.2f%%",
                         symbol, _continuation_break, _bars_rising, _price_vs_3ago)
                return None

        breakeven_move_pct = config.BREAKEVEN_TRIGGER_PCT * 100
        required_15m_pct = breakeven_move_pct * config.BREAKEVEN_FEASIBILITY_MULTIPLIER
        trend_can_reach_be = (
            _last_bullish
            and _bars_rising >= 2
            and _price_vs_3ago >= required_15m_pct
        )
        breakout_can_reach_be = (
            bool(sig.get("above_orb_30", False))
            or (
                bool(sig.get("above_first_bar_high", False))
                and _price_vs_3ago >= breakeven_move_pct
            )
        )
        vwap_can_reach_be = (
            _vwap_reclaim
            and bool(sig.get("above_vwap", False))
            and vol_ratio >= 1.0
        )
        gap_can_reach_be = (
            _gap_go
            and _last_bullish
            and _price_vs_3ago >= breakeven_move_pct
        )
        if not any((trend_can_reach_be, breakout_can_reach_be, vwap_can_reach_be, gap_can_reach_be)):
            reason = (
                f"Breakeven feasibility gate: needs realistic path to +{breakeven_move_pct:.2f}% "
                f"protection trigger; 15m={_price_vs_3ago:+.2f}% "
                f"(need {required_15m_pct:.2f}% trend), bars={_bars_rising}/3, "
                f"vwap_reclaim={_vwap_reclaim}, breakout={bool(sig.get('above_orb_30', False))}"
            )
            self._record_buy_skip(symbol, price, reason, signal_score, "BREAKEVEN_FEASIBILITY")
            log.info(
                "BREAKEVEN feasibility gate %s: 15m=%+.2f%% bars=%d/3 last_bullish=%s",
                symbol, _price_vs_3ago, _bars_rising, _last_bullish,
            )
            return None

        # Hard extension gate: block outright chasing.
        # The score already penalises extension; this veto prevents a high raw score
        # from overriding the warning when price is too far above EMA21.
        _ema21 = float(sig.get("ema21") or 0)
        if _ema21 > 0 and price > 0:
            _ext_pct = (price - _ema21) / _ema21
            if _ext_pct > config.MAX_EMA21_EXTENSION_PCT:
                reason = (f"Extension gate: price {_ext_pct:.1%} above EMA21 "
                          f"(max {config.MAX_EMA21_EXTENSION_PCT:.1%})  chasing, pullback risk")
                self._record_buy_skip(symbol, price, reason, signal_score, "EXTENSION_GATE")
                log.info("EXTENSION gate %s: %.1f%% above EMA21 - blocking chase entry", symbol, _ext_pct * 100)
                return None

        # Opening-thrust staleness gates — early window only, exempt gap_go + vwap_reclaim.
        # Gate 1: price too far above today's open → opening move already done, chasing.
        # Gate 2: price already fading from session high → missed the peak, entering a pullback.
        if gate_ctx["in_early"] and not gate_ctx["gap_go"] and not gate_ctx["vwap_reclaim"]:
            _price_vs_open = float(sig.get("price_vs_open_pct", 0.0))
            if _price_vs_open > config.EARLY_THRUST_MAX_OPEN_PCT:
                reason = (
                    f"Opening thrust gate: price {_price_vs_open:.2f}% above today's open "
                    f"(max {config.EARLY_THRUST_MAX_OPEN_PCT:.2f}%)  opening move done, wait for pullback"
                )
                self._record_buy_skip(symbol, price, reason, signal_score, "OPENING_THRUST_GATE")
                log.info("OPENING THRUST gate %s: %.2f%% above open - late entry", symbol, _price_vs_open)
                return None

            _off_high = float(sig.get("session_high_off_pct", 0.0))
            if _off_high < config.EARLY_THRUST_MAX_OFF_HIGH:
                reason = (
                    f"Session fade gate: price {_off_high:.2f}% below session high "
                    f"(max {config.EARLY_THRUST_MAX_OFF_HIGH:.2f}%)  already fading from peak, wait for base"
                )
                self._record_buy_skip(symbol, price, reason, signal_score, "SESSION_FADE_GATE")
                log.info("SESSION FADE gate %s: %.2f%% off session high - entering pullback", symbol, _off_high)
                return None

        _posture       = (self._daily_plan or {}).get("risk_posture", "normal")
        _spy_ok        = getattr(self, "_spy_trend_ok", True)
        _spy_streak    = getattr(self, "_spy_trend_ok_streak", 1)
        # Require 2 consecutive green SPY scans (~10 min) before allowing new longs
        # after a bearish SPY reading, regardless of posture.  Conservative needs 3.
        _required_streak = 3 if _posture == "conservative" else 2
        spy_blocked = (
            (not _spy_ok or _spy_streak < _required_streak)
            and not gate_ctx["gap_go"]
            and not gate_ctx["vwap_reclaim"]
        )
        if spy_blocked:
            if not _spy_ok:
                reason = "SPY trend gate: market trending down - no long entries"
                log.info("SPY trend gate %s: SPY bearish last 3 bars - skipping long entry", symbol)
            else:
                reason = (f"SPY recovery gate: trend green {_spy_streak}/{_required_streak} scans "
                          f"- need sustained recovery before new longs")
                log.info("SPY recovery gate %s: streak=%d/%d - waiting for sustained recovery",
                         symbol, _spy_streak, _required_streak)
            self._record_buy_skip(symbol, price, reason, signal_score, "SPY_TREND_GATE")
            return None

        quote = self.broker.get_latest_quote(symbol)
        spread_pct = quote["spread_pct"] if quote else None
        limit_price = round(quote["ask"] + 0.01, 2) if quote else None
        if limit_price is None:
            self._record_buy_skip(
                symbol,
                price,
                "No valid quote - skipping to avoid unprotected market entry (IEX data gap or spread >5%)",
                signal_score,
                "NO_QUOTE",
            )
            log.warning("Skip %s - no valid quote from IEX; refusing market-order fallback to avoid slippage", symbol)
            return None

        edgar_veto, edgar_reason = self.edgar.check_fresh_8k(symbol)
        if edgar_veto:
            self._record_buy_skip(symbol, price, f"EDGAR 8-K gate: {edgar_reason}", signal_score, "EDGAR_8K")
            log.warning("EDGAR veto %s: %s", symbol, edgar_reason)
            return None

        ok, reason = self.risk_manager.approve_buy(
            symbol,
            price,
            qty,
            stop_loss,
            settled_cash,
            self._deployed_today,
            num_positions,
            effective_daily_pnl,
            equity,
            self._trades_today,
            rr,
            confidence,
            vol_ratio,
            rsi,
            spread_pct=spread_pct,
            key_levels=self._key_levels_cache.get(symbol),
            min_vol_ratio_override=gate_ctx["vol_floor"],
        )
        if not ok:
            self._record_buy_skip(symbol, price, f"Risk veto: {reason} | {full_reason}", signal_score, "RISK_MANAGER")
            log.info("BUY vetoed %s: %s", symbol, reason)
            return None

        return limit_price

    def _confirm_buy_fill(self, symbol: str, order, price: float, signal_score) -> float | None:
        """Confirm a submitted buy order fill or cancel and audit the failed fill.

        Args:
            symbol: Uppercase ticker.
            order: Broker order object returned by place_bracket_order.
            price: Decision price used as fallback and audit price.
            signal_score: Scanner score used for audit logging.

        Returns:
            Fill price when confirmed, otherwise None.
        """
        order_id = getattr(order, "id", None)
        # Limit orders need more time to fill than market orders — give them 15s.
        is_limit = str(getattr(order, "order_type", "") or getattr(order, "type", "")).lower() == "limit"
        retries, delay = (20, 0.75) if is_limit else (6, 0.5)
        timeout_s = int(retries * delay)
        fill_price = self.broker.get_fill_price(str(order_id), retries=retries, delay=delay) if order_id else None
        if fill_price is not None:
            return fill_price

        if order_id:
            cancel_failed = False
            try:
                self.broker._trade_client.cancel_order_by_id(str(order_id))
                log.warning("BUY %s: order %s not filled after %ds - cancelled", symbol, order_id, timeout_s)
            except Exception as exc:
                cancel_failed = True
                log.warning("Could not cancel order %s for %s: %s - checking broker position", order_id, symbol, exc)
            if cancel_failed:
                try:
                    broker_pos = self.broker.get_positions().get(symbol)
                    if broker_pos is not None:
                        fill_price = float(getattr(broker_pos, "avg_entry_price", None) or price)
                        log.info("BUY %s: cancel failed, broker position confirmed - fill=%.4f", symbol, fill_price)
                except Exception as exc:
                    log.warning("Broker position re-check failed for %s: %s", symbol, exc)

        if fill_price is None:
            self._record_buy_skip(
                symbol,
                price,
                f"Order submitted but fill not confirmed within {timeout_s}s - cancelled",
                signal_score,
                "NO_FILL",
            )
        return fill_price

    def _record_filled_buy(
        self,
        d: dict,
        symbol: str,
        fill_price: float,
        decision_price: float,
        qty: float,
        stop_loss: float,
        take_profit: float,
        settled_cash: float,
        equity: float,
        effective_daily_pnl: float,
        num_positions: int,
        full_reason: str,
        reason_entry: str,
        setup_type_hint: str,
    ) -> float:
        """Persist a confirmed BUY and notify the user.

        Args:
            d: Raw decision dict.
            symbol: Uppercase ticker.
            fill_price: Confirmed broker fill price.
            decision_price: Price used by the algo decision.
            qty: Filled share quantity.
            stop_loss: Protective stop price.
            take_profit: Bracket take-profit placeholder.
            settled_cash: Settled cash available before the buy.
            equity: Account equity for notifications.
            effective_daily_pnl: Realized plus unrealized P&L.
            num_positions: Position count before the buy.
            full_reason: Full persisted decision rationale.
            reason_entry: Short alert rationale.
            setup_type_hint: Scanner setup label.

        Returns:
            Filled cost in dollars.
        """
        slippage_per_sh = fill_price - decision_price
        slippage_dollars = slippage_per_sh * qty
        if abs(slippage_per_sh) > 0.01:
            log.info(
                "Slippage %s: fill=%.4f decision=%.4f diff=%.4f total=$%.2f",
                symbol,
                fill_price,
                decision_price,
                slippage_per_sh,
                slippage_dollars,
            )

        cost = fill_price * qty
        setup_type = d.get("setup_type") or setup_type_hint or None
        with self._state_lock:
            self._deployed_today += cost
            self._trades_today += 1
            self._traded_buckets_today.add(config.SYMBOL_BUCKET.get(symbol, "unknown"))

        self.gfv_tracker.record_buy(symbol, funded_by_settled=settled_cash >= cost)
        self.database.save_position(symbol, fill_price, qty, stop_loss, take_profit, setup_type=setup_type)
        self.database.record_decision(
            symbol,
            "BUY",
            fill_price,
            qty,
            stop_loss,
            take_profit,
            reasoning=full_reason,
            setup_type=setup_type,
            confidence=int(float(d.get("signal_confidence") or 0)),
            slippage_dollars=round(slippage_dollars, 4),
        )
        self.notifier.send_trade_alert(
            action="BUY",
            symbol=symbol,
            price=fill_price,
            qty=qty,
            equity=equity,
            daily_pnl=effective_daily_pnl,
            deployed=self._deployed_today,
            positions_open=num_positions + 1,
            stop_loss=stop_loss,
            take_profit=take_profit,
            setup_type=setup_type,
            reason=reason_entry,
        )
        return cost

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
        preflight = self._preflight_buy(
            d,
            symbol,
            _ss,
            open_symbols,
            effective_daily_pnl,
            dynamic_confidence_bar,
            cooling_symbols,
            suppressed_setups,
        )
        if preflight is None:
            return None

        market_ctx = self._load_buy_market_context(d, symbol, _ss)
        if market_ctx is None:
            return None

        confidence = preflight["confidence"]
        setup_type_hint = preflight["setup_type_hint"]
        price = market_ctx["price"]
        atr = market_ctx["atr"]
        sig = market_ctx["sig"]
        stop_loss = market_ctx["stop_loss"]
        take_profit = market_ctx["take_profit"]

        qty = self._size_buy_order(
            symbol,
            price,
            stop_loss,
            atr,
            confidence,
            settled_cash,
            equity,
            vix_factor,
            kelly_factor,
            positions_snapshot,
            sector_strength,
            _score_lookup,
            _ss,
        )
        if qty is None:
            return None

        limit_price = self._validate_market_entry(
            d,
            symbol,
            price,
            qty,
            stop_loss,
            take_profit,
            sig,
            settled_cash,
            equity,
            effective_daily_pnl,
            num_positions,
            confidence,
            _ss,
            full_reason,
        )
        if limit_price is None:
            return None

        order = self.broker.place_bracket_order(
            symbol, qty, stop_loss, take_profit, limit_price=limit_price)
        if not order:
            return None

        fill_price = self._confirm_buy_fill(symbol, order, price, _ss)
        if fill_price is None:
            return None

        return self._record_filled_buy(
            d,
            symbol,
            fill_price,
            price,
            qty,
            stop_loss,
            take_profit,
            settled_cash,
            equity,
            effective_daily_pnl,
            num_positions,
            full_reason,
            reason_entry,
            setup_type_hint,
        )
