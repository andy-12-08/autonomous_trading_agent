import concurrent.futures as _cf
from datetime import date, datetime

import config
from core.database import log


class TradeCycleMixin:
    """One full scan-and-trade cycle: macro context, universe, algo decisions, and execution."""

    def _gather_macro_factors(self, account_ctx: dict) -> tuple[float, str]:
        """Populate account_ctx with VIX, yield curve, structure, and intraday regime.

        Args:
            account_ctx: Mutable dict of account and context fields for prompts.

        Returns:
            Tuple of (vix_size_factor, regime_string) after compounding VIX with yield curve.
        """
        vix_label, vix_vol, vix_factor = self.market_guard.get_vix_regime()
        account_ctx["vix_regime"] = f"{vix_label} ({vix_vol:.1f}% realized vol, ×{vix_factor:.2f})"
        log.info("VIX regime: %s (realized vol %.1f%%, size ×%.2f)", vix_label, vix_vol, vix_factor)

        yc_signal  = self.yield_curve.get_yield_curve()
        yc_mult    = yc_signal.get("size_multiplier", 1.0)
        vix_factor = round(vix_factor * yc_mult, 4)
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

        return vix_factor, regime

    def _compute_sector_rotation(self, account_ctx: dict) -> dict:
        """Compute sector ETF day-over-day changes and attach formatted strings to account_ctx.

        Args:
            account_ctx: Mutable dict; receives a sector_strength key of label strings.

        Returns:
            Raw sector strength map (bucket to float) used by prioritization.
        """
        sector_etfs = list(self.bucket_manager.SECTOR_ETF_MAP.values())
        etf_bars    = self.broker.get_bars_multi(sector_etfs, "1Day", days=5)
        etf_snaps: dict[str, dict] = {}
        for sym, df in etf_bars.items():
            if len(df) >= 2:
                prev_close = float(df["close"].iloc[-2])
                price      = float(df["close"].iloc[-1])
                if prev_close > 0:
                    etf_snaps[sym] = {
                        "change_pct": round((price - prev_close) / prev_close * 100, 2),
                        "price":      round(price, 2),
                    }
        if not etf_snaps:
            log.warning(
                "Sector rotation: no ETF bar data returned — all sectors defaulting to 0.0%% "
                "(market may be closed or IEX has no data for %s)", sector_etfs)

        sector_str = self.bucket_manager.get_sector_strength(etf_snaps)
        leading    = sorted(sector_str.items(), key=lambda x: x[1], reverse=True)[:3]
        lagging    = sorted(sector_str.items(), key=lambda x: x[1])[:3]
        log.info("Sector rotation — leading: %s | lagging: %s",
                 [(k, f"{v:+.1f}%") for k, v in leading],
                 [(k, f"{v:+.1f}%") for k, v in lagging])
        account_ctx["sector_strength"] = {k: f"{v:+.1f}%" for k, v in sector_str.items()}
        return sector_str

    def _enrich_watchlist(self, watchlist_data: list[dict]) -> list[dict]:
        """Merge parallel alt-data feeds into each candidate dict (mutates the list).

        Args:
            watchlist_data: Non-empty scored candidate list with symbol keys.

        Returns:
            The same list instance with optional news, flow, and pre-market fields added.
        """
        all_syms     = [item["symbol"] for item in watchlist_data]
        top_syms     = all_syms[:25]
        top_syms_opt = all_syms[:30]
        _TIMEOUT     = 20

        pool   = _cf.ThreadPoolExecutor(max_workers=6)
        f_news = pool.submit(self.broker.get_news_headlines, top_syms, 4)
        f_opt  = pool.submit(self.options_flow.get_options_flow, top_syms_opt)
        f_ins  = pool.submit(self.insider_flow.get_recent_insider_buys, top_syms_opt, 7)
        f_dp   = pool.submit(self.dark_pool.get_dark_pool_signals, all_syms)
        f_si   = pool.submit(self.short_interest.get_short_interest, top_syms_opt)
        f_pm   = pool.submit(self.pre_market.get_premarket_data, all_syms)
        _cf.wait([f_news, f_opt, f_ins, f_dp, f_si, f_pm], timeout=_TIMEOUT)
        for _f in (f_news, f_opt, f_ins, f_dp, f_si, f_pm):
            _f.cancel()
        pool.shutdown(wait=False)

        def _safe(fut):
            try:
                return fut.result(timeout=0) if fut.done() else {}
            except Exception:
                return {}

        news_data    = _safe(f_news)
        options_data = _safe(f_opt)
        insider_data = _safe(f_ins)
        dp_data      = _safe(f_dp)
        si_data      = _safe(f_si)
        pm_data      = _safe(f_pm)

        done = sum(1 for f in [f_news, f_opt, f_ins, f_dp, f_si, f_pm] if f.done())
        if done < 6:
            log.warning("Enrichment: only %d/6 calls finished in %ds — slow APIs skipped",
                        done, _TIMEOUT)

        if news_data:
            for item in watchlist_data:
                headlines = news_data.get(item["symbol"])
                if headlines:
                    item["has_catalyst"]   = True
                    item["news_headlines"] = [h["headline"] for h in headlines[:3]]
            log.info("News attached: %d/%d candidates have headlines",
                     sum(1 for i in watchlist_data if i.get("has_catalyst")), len(watchlist_data))

        for item in watchlist_data:
            sym = item["symbol"]
            if sym in options_data:
                item["options_flow"]   = options_data[sym]
            if sym in insider_data:
                item["insider_buying"] = insider_data[sym]
            if sym in dp_data:
                item["dark_pool"]      = dp_data[sym]
            if sym in si_data:
                item["short_interest"] = si_data[sym]
            if sym in pm_data:
                item["pre_market"] = pm_data[sym]
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

        return watchlist_data

    def _pre_filter_candidates(
        self, watchlist_data: list[dict], positions_snapshot: list[dict],
        cooling_symbols: dict, sector_str: dict,
    ) -> tuple[list[dict], list[tuple[str, str]]]:
        """Mechanically veto candidates before the algo engine evaluates them.

        Args:
            watchlist_data: Scored rows entering the AI stage.
            positions_snapshot: Open positions for bucket occupancy checks.
            cooling_symbols: Map of symbol to cooling-off explanation text.
            sector_str: Sector strength floats from _compute_sector_rotation.

        Returns:
            Tuple of kept candidate rows and a parallel list of (symbol, reason) vetoes.
        """
        pre_vetoed: list[tuple[str, str]] = []
        pre_passed: list[dict]            = []

        for item in watchlist_data:
            sym  = item["symbol"]
            conf = item.get("signal_score", 6)

            bucket_ok, bucket_reason = self.bucket_manager.bucket_is_open(
                sym, positions_snapshot, int(conf), sector_strength=sector_str)
            if not bucket_ok:
                pre_vetoed.append((sym, f"bucket: {bucket_reason}"))
                continue

            eb_blocked, eb_reason = self.market_guard.is_earnings_blackout(sym)
            if eb_blocked:
                pre_vetoed.append((sym, f"earnings: {eb_reason}"))
                continue

            if sym in cooling_symbols:
                pre_vetoed.append((sym, f"cooling: {cooling_symbols[sym]}"))
                continue

            # 15-min alignment gate — momentum and gap_and_go setups require the
            # 15-min timeframe to be fully bullish (EMA + VWAP + MACD all positive).
            # Mean-reversion and vwap_reclaim setups are exempt: they work precisely
            # because price is NOT yet aligned on the higher timeframe.
            setup_hint = item.get("setup_type_hint", "momentum")
            if setup_hint in ("momentum", "gap_and_go"):
                b15    = item.get("bias_15min") or {}
                bull15 = sum([bool(b15.get("ema_bull")),
                              bool(b15.get("above_vwap")),
                              bool(b15.get("macd_bull"))])
                if bull15 < 3:
                    pre_vetoed.append((sym,
                        f"15min gate: {bull15}/3 bullish — momentum entries require full 15-min alignment"))
                    continue

            pre_passed.append(item)

        if pre_vetoed:
            log.info("Pre-filter: %d vetoed, %d passed | vetoed: %s",
                     len(pre_vetoed), len(pre_passed),
                     ", ".join(f"{s}({r.split(':')[0]})" for s, r in pre_vetoed[:8]))

        return pre_passed, pre_vetoed

    def _scan_body(self, scan_gen: int = 0):
        """Run one scan: gates, macro, universe, enrichment, Claude, then execute_decisions.

        Exits early when the session date does not match (position job resets the day),
        when the market is closed, during the study-only window, on posture blocks,
        or when throttled. Side effects include broker orders when not in dry-run.

        Returns:
            None.
        """
        now   = datetime.now(self._ET)
        log.info("====[ SCAN AND TRADE | %s ]====", now.strftime("%H:%M:%S"))
        today = datetime.now(self._ET).date().isoformat()

        if today != self._session_date:
            return

        hour, minute = now.hour, now.minute

        if self._force_run:
            log.info("SCAN: force mode — bypassing market-hours and study gates")
            self._study_complete = True
        else:
            in_premarket_study = self.is_in_study_window(hour, minute)
            if not self.broker.is_market_open() and not in_premarket_study:
                return

            if (hour == config.MARKET_CLOSE_HOUR and minute >= config.MARKET_CLOSE_MIN) or \
               hour > config.MARKET_CLOSE_HOUR:
                return

            cur_min     = hour * 60 + minute
            study_start = config.STUDY_START_HOUR * 60 + config.STUDY_START_MIN
            if cur_min < study_start:
                return

            if not self._study_complete:
                cached = self.market_analyst.load_todays_plan()
                if cached:
                    self._daily_plan    = cached
                    self._study_complete = True
                    log.info("SCAN: loaded cached daily plan")
                else:
                    log.info("SCAN: skipped — morning study not yet complete")
                    return

            if self.is_in_study_window(hour, minute):
                return

        broker_acct  = self.broker.get_account()
        equity       = float(getattr(broker_acct, "equity", None) or config.ACCOUNT_SIZE)
        raw_settled  = float(getattr(broker_acct, "non_marginable_buying_power", None)
                             or getattr(broker_acct, "cash", None) or equity)
        settled_cash = self.gfv_tracker.get_available_settled_cash(raw_settled, self._deployed_today)
        exposure_pct = round(self._deployed_today / equity * 100, 1) if equity else 0

        account_ctx = {
            "settled_cash":       round(settled_cash, 2),
            "total_equity":       round(equity, 2),
            "daily_pnl_realized": round(self._daily_pnl, 2),
            "daily_pnl_unrealized": 0.0,
            "daily_pnl_effective":  0.0,
            "daily_pnl_pct":        0.0,
            "deployed_today":     round(self._deployed_today, 2),
            "total_exposure_pct": exposure_pct,
            "available_today":    round(max(0, config.MAX_DAILY_CAPITAL - self._deployed_today), 2),
            "open_positions":     len(self.broker.get_positions()),
            "trades_today":       self._trades_today,
            "trades_remaining":   max(0, config.MAX_TRADES_PER_DAY - self._trades_today),
            "drawdown_limit":     config.DAILY_DRAWDOWN_LIMIT,
            "exposure_cap_pct":   int(config.MAX_TOTAL_EXPOSURE_PCT * 100),
            "max_daily_capital":  config.MAX_DAILY_CAPITAL,
        }

        if account_ctx["available_today"] <= 0:
            if account_ctx["open_positions"] > 0:
                log.info(
                    "Daily capital exhausted ($%.0f deployed) — "
                    "%d open position(s) handled by position manager, skipping full scan",
                    self._deployed_today, account_ctx["open_positions"])
            else:
                log.info(
                    "Daily capital exhausted ($%.0f deployed) and no open positions "
                    "— skipping scan until tomorrow", self._deployed_today)
            return

        in_high_vol_window = self.is_high_volume_window(hour, minute)
        midday             = not in_high_vol_window

        if self._daily_plan and self._daily_plan.get("risk_posture") == "stand_aside":
            is_fomc = (
                self._daily_plan.get("is_fomc_day", False) or
                any("FOMC" in str(w) for w in (self._daily_plan.get("special_warnings") or []))
            )
            if is_fomc and (hour > 14 or (hour == 14 and minute >= 30)):
                is_fomc = False
                log.info("FOMC post-announcement window (>14:30 ET) — unlocking SPY override check")

            # NFP/CPI/GDP print at 8:30 ET; market absorbs the data within ~2h.
            # After 10:30 ET, downgrade to conservative so the prime window isn't lost.
            # FOMC keeps its own stricter 14:30 ET lock.
            if not is_fomc and (hour > 10 or (hour == 10 and minute >= 30)):
                self._daily_plan["risk_posture"] = "conservative"
                log.warning("Macro unlock: past 10:30 ET — downgrading stand_aside → conservative "
                            "(NFP/CPI dust settled; FOMC would stay locked)")

            if self._daily_plan.get("risk_posture") == "stand_aside":
                spy_bars = self.broker.get_bars("SPY", "5Min", days=1)
                if not spy_bars.empty and not is_fomc:
                    spy_now  = float(spy_bars["close"].iloc[-1])
                    spy_open = float(spy_bars["open"].iloc[0])
                    spy_gain = (spy_now - spy_open) / spy_open * 100
                    if spy_gain >= 0.5:
                        self._daily_plan["risk_posture"] = "conservative"
                        log.warning("Macro override: SPY +%.2f%% since open — downgrading "
                                    "stand_aside → conservative.", spy_gain)
                    else:
                        reason = (self._daily_plan.get("special_warnings") or ["macro/market conditions"])[0]
                        log.warning("SCAN POSTURE: STAND_ASIDE — %s", reason[:120])
                        return
                elif is_fomc:
                    reason = (self._daily_plan.get("special_warnings") or ["FOMC day — locked until 14:30 ET"])[0]
                    log.warning("SCAN POSTURE: STAND_ASIDE (FOMC locked) — %s", reason[:120])
                    return
                else:
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

        cur_min = hour * 60 + minute
        if 11 * 60 + 30 <= cur_min < 14 * 60:
            if self._last_full_scan_ts is not None:
                elapsed = (now - self._last_full_scan_ts).total_seconds()
                if elapsed < 720:
                    log.info("Midday throttle: last scan %.0fs ago — skipping (12-min interval)", elapsed)
                    return

        cb_ok, cb_reason = self.market_guard.check_circuit_breaker()
        account_ctx["circuit_breaker"] = cb_reason if not cb_ok else "OK"
        if not cb_ok:
            log.warning("CIRCUIT BREAKER active — no new entries this scan")

        vix_factor, regime = self._gather_macro_factors(account_ctx)

        recent_decisions  = self.database.get_recent_decisions(200)
        dyn_conf_bar      = self.expectancy_engine.compute_dynamic_confidence_bar(recent_decisions)
        cooling_symbols   = self.expectancy_engine.get_cooling_symbols(recent_decisions)
        suppressed_setups = self.expectancy_engine.get_suppressed_setups(recent_decisions)
        if cooling_symbols:
            log.info("Cooling-off symbols: %s", list(cooling_symbols.keys()))
        if suppressed_setups:
            log.warning("Suppressed setups (Rule 19): %s", list(suppressed_setups.keys()))

        posture_now = (self._daily_plan or {}).get("risk_posture", "normal")
        if (posture_now != "stand_aside" and cb_ok and
                equity > 0 and self._deployed_today < config.MIN_TOTAL_EXPOSURE_PCT * equity):
            dyn_conf_bar = max(config.MIN_SIGNAL_CONFIDENCE, dyn_conf_bar - 1)
            log.info("Exposure floor: deployed %.0f%% below %.0f%% minimum — lowering confidence bar to %d",
                     exposure_pct, config.MIN_TOTAL_EXPOSURE_PCT * 100, dyn_conf_bar)
        account_ctx["dynamic_confidence_bar"] = dyn_conf_bar

        positions_snapshot  = self.build_positions_snapshot()
        unrealized_pnl      = sum(p.get("pnl", 0) for p in positions_snapshot)
        effective_daily_pnl = self._daily_pnl + unrealized_pnl
        account_ctx["daily_pnl_unrealized"] = round(unrealized_pnl, 2)
        account_ctx["daily_pnl_effective"]  = round(effective_daily_pnl, 2)
        account_ctx["daily_pnl_pct"]        = round(effective_daily_pnl / equity * 100, 2) if equity else 0

        universe       = self.screener.build_universe()
        watchlist_data = self.build_watchlist_data(
            self._daily_plan, midday=midday, universe=universe, regime=regime)
        bucket_report  = self.bucket_manager.build_bucket_report(positions_snapshot)

        sector_str = self._compute_sector_rotation(account_ctx)

        _now_ctx   = datetime.now(self._ET)
        _cur_min_c = _now_ctx.hour * 60 + _now_ctx.minute
        _open_min  = config.MARKET_OPEN_HOUR * 60 + config.MARKET_OPEN_MIN
        _early_end = config.EARLY_WINDOW_END_HOUR * 60 + config.EARLY_WINDOW_END_MIN
        _in_early  = _open_min <= _cur_min_c < _early_end
        account_ctx["early_window"]      = _in_early
        account_ctx["vol_ratio_floor"]   = (
            config.GAP_AND_GO_VOL_RATIO if _in_early else config.MIN_VOL_RATIO_ENTRY)
        account_ctx["early_window_note"] = (
            f"EARLY WINDOW ACTIVE (9:35–10:30 ET): vol_ratio floor relaxed to "
            f"{config.EARLY_WINDOW_VOL_RATIO} (general) or {config.GAP_AND_GO_VOL_RATIO} "
            f"(gap >= {config.GAP_AND_GO_MIN_VOL_PCT}% + above VWAP). "
            f"Do NOT auto-SKIP on vol_ratio < 1.0 this cycle — let the risk manager decide."
            if _in_early else
            "Normal session: standard vol_ratio floor applies (>= 0.7)."
        )

        watchlist_data = self.bucket_manager.prioritize_watchlist(
            watchlist_data, positions_snapshot, self._traded_buckets_today,
            sector_strength=sector_str)

        if watchlist_data:
            watchlist_data = self._enrich_watchlist(watchlist_data)

        signal_score_lookup = {
            item["symbol"]: item["signal_score"]
            for item in watchlist_data
            if item.get("signal_score") is not None
        }

        if not watchlist_data and not positions_snapshot:
            log.info("No candidates and no open positions — skipping algo call this scan")
            return

        pre_passed, _ = self._pre_filter_candidates(
            watchlist_data, positions_snapshot, cooling_symbols, sector_str)

        with self._state_lock:
            self._daily_pre_passed.update(item["symbol"] for item in pre_passed)

        decisions = self.algo_engine.make_decisions(pre_passed, positions_snapshot)
        log.info("Decisions: %d (ALGO)", len(decisions))

        kelly = self.expectancy_engine.compute_kelly_factor(recent_decisions)
        if kelly != 1.0:
            log.info("Kelly factor %.3f (n=%d closed trades)", kelly, len(recent_decisions))

        pnl_factor = 1.0
        if equity > 0:
            pnl_pct = effective_daily_pnl / equity
            for threshold, factor in config.INTRADAY_PNL_TIERS:
                if pnl_pct <= threshold:
                    pnl_factor = factor
                    log.warning(
                        "Intraday PnL degradation: daily P&L %.1f%% ≤ %.1f%% — sizing ×%.2f",
                        pnl_pct * 100, threshold * 100, factor)
                    break
        vix_factor = round(vix_factor * pnl_factor, 4)

        if self._scan_generation != scan_gen:
            log.warning(
                "Scan gen %d abandoned — skipping execute_decisions to prevent stale orders",
                scan_gen,
            )
            return

        with self._broker_lock:
            self.execute_decisions(
                decisions, positions_snapshot, settled_cash, equity,
                effective_daily_pnl=effective_daily_pnl,
                dynamic_confidence_bar=dyn_conf_bar,
                vix_factor=vix_factor, kelly_factor=kelly,
                cooling_symbols=cooling_symbols,
                suppressed_setups=suppressed_setups,
                signal_score_lookup=signal_score_lookup,
                sector_strength=sector_str)

        self._last_full_scan_ts = now
