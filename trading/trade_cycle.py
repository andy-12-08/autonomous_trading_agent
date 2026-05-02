from datetime import date, datetime

import config
from core.database import log


class TradeCycleMixin:
    def _scan_body(self):
        """Run one full scan: universe, scoring, alt data, agent, execute_decisions.

        Returns:
            None. Exits early when outside session windows or throttled.
        """
        now   = datetime.now(self._ET)
        log.info("====[ SCAN AND TRADE | %s ]====", now.strftime("%H:%M:%S"))
        today = date.today().isoformat()

        if today != self._session_date:
            return  # position management job handles the reset

        hour, minute = now.hour, now.minute

        in_premarket_study = self.is_in_study_window(hour, minute)
        if not self.broker.is_market_open() and not in_premarket_study:
            return

        if (hour == config.MARKET_CLOSE_HOUR and minute >= config.MARKET_CLOSE_MIN) or \
           hour > config.MARKET_CLOSE_HOUR:
            return  # EOD handled by position management

        cur_min     = hour * 60 + minute
        study_start = config.STUDY_START_HOUR * 60 + config.STUDY_START_MIN
        if cur_min < study_start:
            return

        # Don't scan until morning study is complete.
        # Try loading a cached plan first — handles the race where the scan job fires
        # at the same instant as position management on startup.
        if not self._study_complete:
            cached = self.market_analyst.load_todays_plan()
            if cached:
                self._daily_plan    = cached
                self._study_complete = True
                log.info("SCAN: loaded cached daily plan")
            else:
                log.info("SCAN: skipped — morning study not yet complete")
                return

        # Skip the study window itself — scanning during study is pointless
        if self.is_in_study_window(hour, minute):
            return

        # Account state
        broker_acct  = self.broker.get_account()
        equity       = float(getattr(broker_acct, "equity", None) or config.ACCOUNT_SIZE)
        raw_settled  = float(getattr(broker_acct, "non_marginable_buying_power", None)
                             or getattr(broker_acct, "cash", None) or equity)
        settled_cash = self.gfv_tracker.get_available_settled_cash(raw_settled, self._deployed_today)
        exposure_pct = round(self._deployed_today / equity * 100, 1) if equity else 0

        account_ctx = {
            "settled_cash":          round(settled_cash, 2),
            "total_equity":          round(equity, 2),
            "daily_pnl_realized":    round(self._daily_pnl, 2),
            "daily_pnl_unrealized":  0.0,
            "daily_pnl_effective":   0.0,
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

        in_high_vol_window = self.is_high_volume_window(hour, minute)
        midday             = not in_high_vol_window

        # Posture check: surface stand_aside / conservative every scan
        if self._daily_plan and self._daily_plan.get("risk_posture") == "stand_aside":
            is_fomc = (
                self._daily_plan.get("is_fomc_day", False) or
                any("FOMC" in str(w) for w in (self._daily_plan.get("special_warnings") or []))
            )
            # FOMC time-based unlock: Fed decisions release at 14:00 ET.
            # 30 min after the announcement the dust has settled — allow the SPY
            # override to fire so the bot can trade the post-FOMC directional move.
            if is_fomc and (hour > 14 or (hour == 14 and minute >= 30)):
                is_fomc = False
                log.info("FOMC post-announcement window (>14:30 ET) — unlocking SPY override check")

            spy_bars = self.broker.get_bars("SPY", "5Min", days=1)
            if not spy_bars.empty and not is_fomc:
                spy_now  = float(spy_bars["close"].iloc[-1])
                spy_open = float(spy_bars["open"].iloc[0])
                spy_gain = (spy_now - spy_open) / spy_open * 100
                if spy_gain >= 0.5:
                    self._daily_plan["risk_posture"] = "conservative"
                    log.warning(
                        "Macro override: SPY +%.2f%% since open — downgrading "
                        "stand_aside → conservative.", spy_gain
                    )
                    # fall through — continue scan in conservative mode
                else:
                    reason = (self._daily_plan.get("special_warnings") or ["macro/market conditions"])[0]
                    log.warning("SCAN POSTURE: STAND_ASIDE — %s", reason[:120])
                    return
            elif is_fomc:
                reason = (self._daily_plan.get("special_warnings") or ["FOMC day — locked until 14:30 ET"])[0]
                log.warning("SCAN POSTURE: STAND_ASIDE (FOMC locked) — %s", reason[:120])
                return
            else:
                # SPY bars unavailable — cannot verify recovery; honour stand-aside
                log.warning("SCAN POSTURE: STAND_ASIDE — SPY bars unavailable, staying out")
                return
        elif self._daily_plan and self._daily_plan.get("risk_posture") == "conservative":
            reason = (self._daily_plan.get("special_warnings") or ["macro/market conditions"])[0]
            log.warning("SCAN POSTURE: CONSERVATIVE — %s", reason[:120])

        log.info("--- SCAN %s | vol_window=%s pnl=%.0f deployed=%.0f (%.1f%%) trades=%d/%d ---",
                 now.strftime("%H:%M"), "YES" if in_high_vol_window else "MIDDAY",
                 self._daily_pnl, self._deployed_today, exposure_pct,
                 self._trades_today, config.MAX_TRADES_PER_DAY)

        if midday:
            log.info("MIDDAY scan: threshold=%.1f", config.MIDDAY_MIN_SIGNAL_SCORE)

        # Midday scan throttle (11:30–14:00 ET)
        # The 11:30–14:00 window is statistically the weakest period for momentum setups.
        # Halve the effective scan rate (12-min interval) to save Claude API budget.
        cur_min = hour * 60 + minute
        if 11 * 60 + 30 <= cur_min < 14 * 60:
            if self._last_full_scan_ts is not None:
                elapsed = (now - self._last_full_scan_ts).total_seconds()
                if elapsed < 720:
                    log.info("Midday throttle: last scan %.0fs ago — skipping (12-min midday interval)", elapsed)
                    return

        # Circuit breaker
        cb_ok, cb_reason = self.market_guard.check_circuit_breaker()
        account_ctx["circuit_breaker"] = cb_reason if not cb_ok else "OK"
        if not cb_ok:
            log.warning("CIRCUIT BREAKER active — no new entries this scan")

        # VIX + market structure + intraday regime
        vix_label, vix_vol, vix_factor = self.market_guard.get_vix_regime()
        account_ctx["vix_regime"] = f"{vix_label} ({vix_vol:.1f}% realized vol, ×{vix_factor:.2f})"
        log.info("VIX regime: %s (realized vol %.1f%%, size ×%.2f)", vix_label, vix_vol, vix_factor)

        # Yield curve + credit spread — macro size multiplier
        yc_signal  = self.yield_curve.get_yield_curve()
        yc_mult    = yc_signal.get("size_multiplier", 1.0)
        vix_factor = round(vix_factor * yc_mult, 4)   # compound with VIX factor
        account_ctx["yield_curve"] = {
            "signal":          yc_signal.get("signal", "normal"),
            "spread_10y_3m":   yc_signal.get("spread_10y_3m"),
            "size_multiplier": yc_mult,
            "note":            yc_signal.get("note", ""),
        }
        if yc_mult < 1.0:
            log.warning("Yield curve %s — size ×%.2f (effective VIX+YC ×%.2f)",
                        yc_signal.get("signal", "").upper(), yc_mult, vix_factor)

        mkt_structure = self.market_guard.get_market_structure()
        if mkt_structure:
            account_ctx["market_structure"] = mkt_structure
            posture = mkt_structure.get("market_posture", "unknown")
            log.info("Market posture: %s | SPY=%.2f vs PDH=%s PDL=%s",
                     posture,
                     mkt_structure.get("spy_price", 0),
                     mkt_structure.get("spy_prev_day_high", "?"),
                     mkt_structure.get("spy_prev_day_low", "?"))

        regime_info = self.market_guard.get_intraday_regime()
        regime      = regime_info.get("regime", "ranging")
        account_ctx["intraday_regime"] = regime_info.get("note", regime)
        log.info("Intraday regime: %s | %s", regime.upper(), regime_info.get("note", ""))

        # Recent decisions, cooling, suppressed setups
        recent_decisions  = self.database.get_recent_decisions(200)
        dyn_conf_bar      = self.expectancy_engine.compute_dynamic_confidence_bar(recent_decisions)
        cooling_symbols   = self.expectancy_engine.get_cooling_symbols(recent_decisions)
        suppressed_setups = self.expectancy_engine.get_suppressed_setups(recent_decisions)
        if cooling_symbols:
            log.info("Cooling-off symbols: %s", list(cooling_symbols.keys()))
        if suppressed_setups:
            log.warning("Suppressed setups (Rule 19): %s", list(suppressed_setups.keys()))

        # Exposure floor enforcement (MIN_TOTAL_EXPOSURE_PCT)
        # If deployed capital is below the 15% floor AND conditions are OK, lower
        # the confidence bar by 1 so valid setups aren't blocked by an overly
        # conservative dynamic bar. Don't apply during stand_aside or circuit breaker.
        posture_now = (self._daily_plan or {}).get("risk_posture", "normal")
        if (posture_now != "stand_aside" and cb_ok and
                equity > 0 and self._deployed_today < config.MIN_TOTAL_EXPOSURE_PCT * equity):
            dyn_conf_bar = max(config.MIN_SIGNAL_CONFIDENCE, dyn_conf_bar - 1)
            log.info("Exposure floor: deployed %.0f%% below %.0f%% minimum — lowering confidence bar to %d",
                     exposure_pct, config.MIN_TOTAL_EXPOSURE_PCT * 100, dyn_conf_bar)

        account_ctx["dynamic_confidence_bar"] = dyn_conf_bar

        # Fresh positions snapshot (may have changed since position management ran)
        positions_snapshot  = self.build_positions_snapshot()
        unrealized_pnl      = sum(p.get("pnl", 0) for p in positions_snapshot)
        effective_daily_pnl = self._daily_pnl + unrealized_pnl
        account_ctx["daily_pnl_unrealized"] = round(unrealized_pnl, 2)
        account_ctx["daily_pnl_effective"]  = round(effective_daily_pnl, 2)
        account_ctx["daily_pnl_pct"]        = round(effective_daily_pnl / equity * 100, 2) if equity else 0

        # Build dynamic universe + score signals
        universe       = self.screener.build_universe()
        watchlist_data = self.build_watchlist_data(self._daily_plan, midday=midday,
                                                   universe=universe, regime=regime)
        bucket_report  = self.bucket_manager.build_bucket_report(positions_snapshot)

        # Sector rotation: compute ETF daily change from IEX bars
        # Snapshots require SIP (paid feed); bars work fine on IEX for sector ETFs.
        # Use 2 daily bars per ETF: bars[-2]=prev close, bars[-1]=today's latest bar.
        sector_etfs = list(self.bucket_manager.SECTOR_ETF_MAP.values())
        etf_bars    = self.broker.get_bars_multi(sector_etfs, "1Day", days=3)
        etf_snaps   = {}
        for sym, df in etf_bars.items():
            if len(df) >= 2:
                prev_close = float(df["close"].iloc[-2])
                price      = float(df["close"].iloc[-1])
                if prev_close > 0:
                    etf_snaps[sym] = {
                        "change_pct": round((price - prev_close) / prev_close * 100, 2),
                        "price":      round(price, 2),
                    }
        sector_str = self.bucket_manager.get_sector_strength(etf_snaps)
        leading = sorted(sector_str.items(), key=lambda x: x[1], reverse=True)[:3]
        lagging = sorted(sector_str.items(), key=lambda x: x[1])[:3]
        log.info("Sector rotation — leading: %s | lagging: %s",
                 [(k, f"{v:+.1f}%") for k, v in leading],
                 [(k, f"{v:+.1f}%") for k, v in lagging])
        account_ctx["sector_strength"] = {k: f"{v:+.1f}%" for k, v in sector_str.items()}

        # Tell Claude whether it is in the early-window so it applies the right vol_ratio floor
        _now_ctx        = datetime.now(self._ET)
        _cur_min_ctx    = _now_ctx.hour * 60 + _now_ctx.minute
        _open_min_ctx   = config.MARKET_OPEN_HOUR * 60 + config.MARKET_OPEN_MIN
        _early_end_ctx  = config.EARLY_WINDOW_END_HOUR * 60 + config.EARLY_WINDOW_END_MIN
        _in_early_ctx   = _open_min_ctx <= _cur_min_ctx < _early_end_ctx
        account_ctx["early_window"] = _in_early_ctx
        account_ctx["vol_ratio_floor"] = (
            config.GAP_AND_GO_VOL_RATIO if _in_early_ctx else config.MIN_VOL_RATIO_ENTRY
        )
        account_ctx["early_window_note"] = (
            f"EARLY WINDOW ACTIVE (9:35–10:30 ET): vol_ratio floor relaxed to "
            f"{config.EARLY_WINDOW_VOL_RATIO} (general) or {config.GAP_AND_GO_VOL_RATIO} "
            f"(gap >= {config.GAP_AND_GO_MIN_VOL_PCT}% + above VWAP). "
            f"Do NOT auto-SKIP on vol_ratio < 1.0 this cycle — let the risk manager decide."
            if _in_early_ctx else
            "Normal session: standard vol_ratio floor applies (>= 0.7)."
        )

        watchlist_data = self.bucket_manager.prioritize_watchlist(
            watchlist_data, positions_snapshot, self._traded_buckets_today,
            sector_strength=sector_str,
        )

        if watchlist_data:
            import concurrent.futures as _cf

            all_syms     = [item["symbol"] for item in watchlist_data]
            top_syms     = all_syms[:25]
            top_syms_opt = all_syms[:30]
            _ENRICH_TIMEOUT = 20  # seconds max — any slower API is skipped

            # Run all enrichment calls in parallel; hard 20-second total wall-clock cap.
            # A hung API (EDGAR, FINRA, options feed) will be abandoned — the scan
            # continues with empty data for that source rather than timing out.
            _pool = _cf.ThreadPoolExecutor(max_workers=6)
            _f_news = _pool.submit(self.broker.get_news_headlines, top_syms, 4)
            _f_opt  = _pool.submit(self.options_flow.get_options_flow, top_syms_opt)
            _f_ins  = _pool.submit(self.insider_flow.get_recent_insider_buys, top_syms_opt, 7)
            _f_dp   = _pool.submit(self.dark_pool.get_dark_pool_signals, all_syms)
            _f_si   = _pool.submit(self.short_interest.get_short_interest, top_syms_opt)
            _f_pm   = _pool.submit(self.pre_market.get_premarket_data, all_syms)
            _cf.wait([_f_news, _f_opt, _f_ins, _f_dp, _f_si, _f_pm], timeout=_ENRICH_TIMEOUT)
            _pool.shutdown(wait=False)  # abandon any threads that are still running

            def _safe(fut):
                try:
                    return fut.result(timeout=0) if fut.done() else {}
                except Exception:
                    return {}

            news_data    = _safe(_f_news)
            options_data = _safe(_f_opt)
            insider_data = _safe(_f_ins)
            dp_data      = _safe(_f_dp)
            si_data      = _safe(_f_si)
            pm_data      = _safe(_f_pm)

            _done = sum(1 for f in [_f_news, _f_opt, _f_ins, _f_dp, _f_si, _f_pm] if f.done())
            if _done < 6:
                log.warning("Enrichment: only %d/6 calls finished in %ds — slow APIs skipped",
                            _done, _ENRICH_TIMEOUT)

            if news_data:
                for item in watchlist_data:
                    headlines = news_data.get(item["symbol"])
                    if headlines:
                        item["has_catalyst"]   = True
                        item["news_headlines"] = [h["headline"] for h in headlines[:3]]
                log.info("News attached: %d/%d candidates have headlines",
                         sum(1 for i in watchlist_data if i.get("has_catalyst")), len(watchlist_data))

            # Merge enrichment data into candidates
            for item in watchlist_data:
                sym = item["symbol"]
                if sym in options_data:
                    item["options_flow"]    = options_data[sym]
                if sym in insider_data:
                    item["insider_buying"]  = insider_data[sym]
                if sym in dp_data:
                    item["dark_pool"]       = dp_data[sym]
                if sym in si_data:
                    item["short_interest"]  = si_data[sym]
                if sym in pm_data:
                    item["pre_market"]      = pm_data[sym]
                    # Merge pre-market high/low into key levels so risk_manager uses them
                    kl = self._key_levels_cache.get(sym) or {}
                    kl["pre_market_high"] = pm_data[sym]["pm_high"]
                    kl["pre_market_low"]  = pm_data[sym]["pm_low"]
                    self._key_levels_cache[sym] = kl

            if options_data:
                unusual = [s for s, d in options_data.items() if d.get("unusual_calls")]
                log.info("Options flow: %d/%d have data | unusual calls: %s",
                         len(options_data), len(top_syms_opt), unusual or "none")
            if insider_data:
                log.info("Insider buying detected: %s", list(insider_data.keys()))
            if dp_data:
                log.info(self.dark_pool.dark_pool_summary(all_syms))
            if si_data:
                log.info(self.short_interest.short_interest_summary(top_syms_opt))
            if pm_data:
                log.info(self.pre_market.premarket_summary(all_syms))

        signal_score_lookup = {
            item["symbol"]: item["signal_score"]
            for item in watchlist_data
            if item.get("signal_score") is not None
        }

        if not watchlist_data and not positions_snapshot:
            log.info("No candidates and no open positions — skipping AI call this scan")
            return

        # Pre-Claude mechanical filter
        # Remove candidates that fail objective, code-enforceable rules before Claude
        # sees them. This keeps Claude focused on quality decisions and eliminates the
        # cascade where Claude avoids an entire sector because it reserved the slot for
        # a candidate that later gets vetoed by the risk manager.
        #
        # Rules moved here (deterministic, no price-action judgment needed):
        #   - Sector bucket already occupied by an ACTUAL open position
        #   - Earnings blackout (within 2 calendar days of report)
        #   - Symbol cooling (recent win-rate too low)
        pre_vetoed = []
        pre_passed = []
        for item in watchlist_data:
            sym  = item["symbol"]
            conf = item.get("signal_score", 6)

            # Bucket occupied by an actual open position?
            bucket_ok, bucket_reason = self.bucket_manager.bucket_is_open(
                sym, positions_snapshot, int(conf), sector_strength=sector_str
            )
            if not bucket_ok:
                pre_vetoed.append((sym, f"bucket: {bucket_reason}"))
                continue

            # Earnings blackout?
            eb_blocked, eb_reason = self.market_guard.is_earnings_blackout(sym)
            if eb_blocked:
                pre_vetoed.append((sym, f"earnings: {eb_reason}"))
                continue

            # Symbol cooling?
            if sym in cooling_symbols:
                pre_vetoed.append((sym, f"cooling: {cooling_symbols[sym]}"))
                continue

            pre_passed.append(item)

        if pre_vetoed:
            log.info("Pre-Claude filter: %d vetoed, %d sent to AI | vetoed: %s",
                     len(pre_vetoed), len(pre_passed),
                     ", ".join(f"{s}({r.split(':')[0]})" for s, r in pre_vetoed[:8]))

        # Accumulate survivors for dynamic watchlist — saved at EOD for tomorrow's news fetch
        self._daily_pre_passed.update(item["symbol"] for item in pre_passed)

        # Claude decision
        # Cap at 20 candidates — already ranked by signal score; more than 20 bloats
        # the prompt without improving decision quality, and risks API timeout.
        ai_candidates = pre_passed[:20]
        if len(pre_passed) > 20:
            log.info("Trimmed candidates: %d → 20 for AI prompt", len(pre_passed))
        decisions = self.trading_agent.ask_agent(
            ai_candidates, positions_snapshot, account_ctx,
            self.database.get_recent_decisions(30), self._daily_plan, bucket_report,
        )
        log.info("AI returned %d decisions", len(decisions))

        # Kelly Criterion sizing factor (uses the same recent_decisions already loaded)
        kelly = self.expectancy_engine.compute_kelly_factor(recent_decisions)
        if kelly != 1.0:
            log.info("Kelly factor %.3f (n=%d closed trades)", kelly, len(recent_decisions))

        # Execute — lock while updating shared counters
        self.execute_decisions(decisions, positions_snapshot, settled_cash, equity,
                               effective_daily_pnl=effective_daily_pnl,
                               dynamic_confidence_bar=dyn_conf_bar,
                               vix_factor=vix_factor, kelly_factor=kelly,
                               cooling_symbols=cooling_symbols,
                               suppressed_setups=suppressed_setups,
                               signal_score_lookup=signal_score_lookup,
                               sector_strength=sector_str)

        self._last_full_scan_ts = now
