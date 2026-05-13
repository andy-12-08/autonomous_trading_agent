"""
Options trade cycle mixin: one complete scan cycle — macro context, universe
building, IV data gathering, enrichment, decision engine, and execution.

Cycle flow:
  1. Time and market gates (study window, EOD, market hours, throttle)
  2. Macro context: VIX regime, yield curve, intraday market structure
  3. Universe: screener builds the candidate pool
  4. IV data: bulk IV regime fetch via IVAnalyzer
  5. Enrichment: options flow, dark pool, pre-market, earnings flags
  6. Decision engine: OptionsDecisionEngine maps candidates to strategies
  7. Execution: OptionsExecutorMixin submits approved orders
"""

from __future__ import annotations

import concurrent.futures as _cf
from datetime import date, datetime

import config
from core.database import log


# Catalyst keywords carried over from the equity engine — same logic applies
# to options: an FDA approval before selling a straddle is a disaster.
_CAT_HIGH = [
    "fda approv", "fda clearance", "breakthrough therapy", "acquisition", "merger",
    "buyout", "takeover", "earnings beat", "revenue beat", "record revenue",
    "raised guidance",
]
_CAT_NEG = [
    "fda reject", "fda refusal", "recall", "class action", "lawsuit",
    "sec investigation", "downgrade", "missed", "guidance cut", "lowered guidance",
]


def _score_catalyst(headlines: list[str]) -> tuple[int, str]:
    """Score headlines for catalyst quality (used in earnings-risk enrichment).

    Args:
        headlines: List of raw headline strings.

    Returns:
        Tuple of (score, matching_keyword). Score: 3=major, 2=significant,
        1=minor, -1=negative, 0=none.
    """
    for headline in headlines:
        h = headline.lower()
        for kw in _CAT_NEG:
            if kw in h:
                return -1, kw
        for kw in _CAT_HIGH:
            if kw in h:
                return 3, kw
    return (1, "news present") if headlines else (0, "")


class OptionsTradeCycleMixin:
    """
    One complete scan-and-trade cycle for the options engine.

    Expects these attributes on `self` (set by TradingOrchestrator):
      broker, iv_analyzer, options_flow, algo_engine, market_guard,
      market_analyst, screener, dark_pool, pre_market, yield_curve, edgar,
      database, signal_scorer, dynamic_watchlist,
      _daily_pnl, _session_date, _study_complete, _daily_plan,
      _current_vix, _consecutive_losses, _last_scan_ts, _force_run,
      _ET, _broker_lock, _state_lock, _scan_generation
    """

    def run_position_management(self) -> None:
        """
        Two-minute scheduler entry: study window handling then options monitoring.

        Returns:
            None.
        """
        now  = datetime.now(self._ET)
        hour, minute = now.hour, now.minute

        log.info("====[ POSITION MANAGEMENT | %s ]====", now.strftime("%H:%M:%S"))

        today = datetime.now(self._ET).date().isoformat()
        if today != self._session_date:
            self.reset_daily_state()

        if self._force_run:
            log.info("POSITION MGMT: force mode")
        else:
            close_min = config.MARKET_CLOSE_HOUR * 60 + config.MARKET_CLOSE_MIN
            cur_min   = hour * 60 + minute

            if cur_min >= close_min or hour > config.MARKET_CLOSE_HOUR:
                if not self._eod_done:
                    try:
                        self.eod_close_all_options()
                        self._eod_done = True
                    except Exception as exc:
                        log.error("EOD close failed: %s", exc)
                        return
                    try:
                        self.write_daily_summary()
                    except Exception as exc:
                        log.error("EOD summary failed: %s", exc)
                else:
                    log.info("EOD already completed — skipping")
                return

            in_study = self.is_in_study_window(hour, minute)

            # Morning study phase
            if in_study and not self._study_complete:
                self._run_study_phase(hour, minute)
                return

            if in_study:
                log.info("Study complete — waiting for market open (%02d:%02d ET)",
                         config.STUDY_END_HOUR, config.STUDY_END_MIN)
                return

            if not self._study_complete:
                # Late start — run study immediately
                self._run_study_phase(hour, minute)

            if not self.broker.is_market_open():
                log.info("Market closed — skipping position management")
                return

        # ── Position monitor ──────────────────────────────────────────────────
        try:
            account = self.broker.get_account()
            equity  = float(getattr(account, "equity", None) or config.ACCOUNT_SIZE)
        except Exception:
            equity  = config.ACCOUNT_SIZE

        with self._broker_lock:
            self.monitor_options_positions()

        snapshot = self.build_options_portfolio_snapshot()
        log.info("Position management done — %d open | Δ=%+.1f ν=%+.1f θ=%+.1f pnl=$%.0f",
                 snapshot["open_count"],
                 snapshot["portfolio_delta"],
                 snapshot["portfolio_vega"],
                 snapshot["portfolio_theta"],
                 snapshot["total_pnl"])

    def _scan_body(self, scan_gen: int = 0) -> None:
        """
        Run one complete scan-and-trade cycle.

        Args:
            scan_gen: Generation counter; stale threads skip execution on mismatch.

        Returns:
            None.
        """
        now  = datetime.now(self._ET)
        log.info("====[ SCAN AND TRADE | %s ]====", now.strftime("%H:%M:%S"))
        today = datetime.now(self._ET).date().isoformat()

        if today != self._session_date:
            return

        hour, minute = now.hour, now.minute
        cur_min      = hour * 60 + minute
        close_min    = config.MARKET_CLOSE_HOUR * 60 + config.MARKET_CLOSE_MIN

        if not self._force_run:
            if cur_min >= close_min or hour > config.MARKET_CLOSE_HOUR:
                return

            if not self._study_complete:
                log.info("SCAN: skipped — morning study not yet complete")
                return

            if not self.broker.is_market_open():
                return

            # Throttle: avoid scanning too frequently outside prime windows
            if self._last_scan_ts is not None:
                elapsed = (now - self._last_scan_ts).total_seconds()
                open_min = config.MARKET_OPEN_HOUR * 60 + config.MARKET_OPEN_MIN
                prime_end = config.PRIME_ENTRY_END_HOUR * 60 + config.PRIME_ENTRY_END_MIN
                if open_min <= cur_min <= prime_end:
                    pass  # Scan every tick during prime window
                elif cur_min < 14 * 60:
                    if elapsed < 600:  # 10-min midday throttle
                        log.info("Midday throttle: last scan %.0fs ago — skipping", elapsed)
                        return
                # Power hour 14:00–15:45: scan every tick

        # ── Circuit breaker ───────────────────────────────────────────────────
        cb_ok, cb_reason = self.market_guard.check_circuit_breaker()
        if not cb_ok:
            log.warning("CIRCUIT BREAKER: %s — no new entries", cb_reason)

        # ── Macro context ─────────────────────────────────────────────────────
        vix_factor, market_regime, vix_level = self._gather_macro_context()
        self._current_vix = vix_level

        # ── Account state ─────────────────────────────────────────────────────
        try:
            account = self.broker.get_account()
            equity  = float(getattr(account, "equity", None) or config.ACCOUNT_SIZE)
        except Exception:
            equity  = config.ACCOUNT_SIZE

        open_positions = self.database.get_open_options_positions()
        snapshot       = self.build_options_portfolio_snapshot()

        effective_pnl = self._daily_pnl + snapshot["total_pnl"]

        if effective_pnl <= -config.DAILY_DRAWDOWN_LIMIT:
            log.warning("DRAWDOWN HALT in scan: $%.0f ≤ -$%.0f — no new entries",
                        effective_pnl, config.DAILY_DRAWDOWN_LIMIT)
            return

        # ── Universe ──────────────────────────────────────────────────────────
        universe = self.screener.build_universe()

        watchlist_symbols = self._get_scored_watchlist(universe, market_regime)
        if not watchlist_symbols:
            log.info("No scored candidates above threshold — skipping scan")
            return

        # ── IV data ───────────────────────────────────────────────────────────
        all_syms    = [item["symbol"] for item in watchlist_symbols]
        iv_data_map = self.iv_analyzer.get_bulk_iv_regimes(all_syms)

        # Filter out symbols with no IV data — options bot requires it
        watchlist_symbols = [
            item for item in watchlist_symbols
            if iv_data_map.get(item["symbol"])
        ]
        if not watchlist_symbols:
            log.info("No symbols with valid IV data — skipping scan")
            return

        # ── Enrichment ────────────────────────────────────────────────────────
        watchlist_symbols = self._enrich_candidates(watchlist_symbols)

        # Attach earnings flag from MarketGuard
        for item in watchlist_symbols:
            blocked, _ = self.market_guard.is_earnings_blackout(item["symbol"])
            item["earnings_soon"] = blocked

        # ── SPY move (needed for 0DTE engine) ─────────────────────────────────
        spy_move_pct = self._compute_spy_move_pct()

        # ── Decision engine ───────────────────────────────────────────────────
        decisions = self.algo_engine.make_decisions(
            candidates     = watchlist_symbols,
            open_positions = open_positions,
            iv_data_map    = iv_data_map,
            market_regime  = market_regime,
            spy_move_pct   = spy_move_pct,
            vix_level      = vix_level,
            hour           = hour,
            minute         = minute,
        )

        # ── Stale-scan guard ──────────────────────────────────────────────────
        if self._scan_generation != scan_gen:
            log.warning("Scan gen %d abandoned — skipping execution", scan_gen)
            return

        # ── Execution ─────────────────────────────────────────────────────────
        with self._broker_lock:
            self.execute_options_decisions(
                decisions          = decisions,
                open_positions     = open_positions,
                daily_pnl          = effective_pnl,
                total_equity       = equity,
                consecutive_losses = self._consecutive_losses,
            )

        self._last_scan_ts = now
        log.info("--- SCAN COMPLETE %s | regime=%s VIX=%.1f SPY%+.2f%% ---",
                 now.strftime("%H:%M"), market_regime, vix_level, spy_move_pct)

    # ── Morning study ─────────────────────────────────────────────────────────

    def _run_study_phase(self, hour: int, minute: int) -> None:
        """
        Execute or load the morning study during the pre-market window.

        Args:
            hour:   Current ET hour.
            minute: Current ET minute.

        Returns:
            None.
        """
        log.info("MORNING STUDY WINDOW (%02d:%02d ET) — scanning market context", hour, minute)

        cached = self.market_analyst.load_todays_plan()
        if cached:
            self._daily_plan     = cached
            self._study_complete = True
            log.info("Loaded cached daily plan from DB")
        else:
            try:
                account = self.broker.get_account()
                equity  = float(getattr(account, "equity", None) or config.ACCOUNT_SIZE)
            except Exception:
                equity  = config.ACCOUNT_SIZE

            self._daily_plan = self.market_analyst.run_morning_study({
                "total_equity": equity,
                "daily_pnl":    0.0,
            })
            self._study_complete = True

        # Pre-warm caches during study window so first scan cycle is fast
        log.info("Pre-warming screener universe and IV caches...")
        self.screener.build_universe()
        self.pre_market.get_premarket_data(config.WATCHLIST)

    # ── Macro context ─────────────────────────────────────────────────────────

    def _gather_macro_context(self) -> tuple[float, str, float]:
        """
        Collect VIX regime, intraday structure, and yield curve macro factors.

        Returns:
            Tuple of (vix_size_factor, market_regime, vix_level).
        """
        vix_label, vix_vol, vix_factor = self.market_guard.get_vix_regime()
        vix_level = vix_vol

        try:
            yc         = self.yield_curve.get_yield_curve()
            yc_mult    = yc.get("size_multiplier", 1.0)
            vix_factor = round(vix_factor * yc_mult, 4)
            if yc_mult < 1.0:
                log.warning("Yield curve %s — combined factor ×%.2f",
                            yc.get("signal", ""), vix_factor)
        except Exception:
            pass

        regime_info  = self.market_guard.get_intraday_regime()
        market_regime = regime_info.get("regime", "ranging")
        log.info("Macro: VIX=%s(%.1f%% vol ×%.2f) regime=%s",
                 vix_label, vix_vol, vix_factor, market_regime.upper())

        return vix_factor, market_regime, vix_level

    # ── Watchlist scoring ─────────────────────────────────────────────────────

    def _get_scored_watchlist(
        self, universe: list[str], market_regime: str,
    ) -> list[dict]:
        """
        Score symbols from the universe and return those above the minimum threshold.

        Args:
            universe:      List of ticker symbols from the screener.
            market_regime: Current intraday regime string.

        Returns:
            List of scored candidate dicts with symbol, signal_score,
            indicators, setup_type_hint, and other enrichment fields.
        """
        watchlist_syms = list(dict.fromkeys(config.WATCHLIST + universe))[:80]

        try:
            scored = []
            bars_map = self.broker.get_bars_multi(watchlist_syms, "5Min", days=3)
            for sym, df in bars_map.items():
                if df is None or df.empty:
                    continue
                try:
                    from analysis.indicators import IndicatorEngine
                    ind_engine = IndicatorEngine()
                    df_ind     = ind_engine.compute_indicators(df)
                    sig        = ind_engine.get_signal_summary(df_ind) if not df_ind.empty else {}

                    from analysis.signal_scorer import SignalScorer
                    score, evidence = SignalScorer.score_setup(sig)

                    scored.append({
                        "symbol":          sym,
                        "signal_score":    score,
                        "signal_class":    SignalScorer.classify(score),
                        "signal_evidence": evidence,
                        "indicators":      sig,
                        "setup_type_hint": "momentum",
                    })
                except Exception as exc:
                    log.debug("Score failed %s: %s", sym, exc)

            scored.sort(key=lambda x: x["signal_score"], reverse=True)
            log.info("Scored watchlist: %d/%d above threshold", len(scored), len(watchlist_syms))
            return scored[:50]

        except Exception as exc:
            log.warning("_get_scored_watchlist failed: %s", exc)
            return []

    # ── Enrichment ────────────────────────────────────────────────────────────

    def _enrich_candidates(self, candidates: list[dict]) -> list[dict]:
        """
        Merge parallel alt-data feeds into each candidate dict.

        Fetches options flow, dark pool, pre-market, and news in parallel
        with a 20-second timeout. Slow APIs are skipped gracefully.

        Args:
            candidates: Scored candidate dicts with symbol keys.

        Returns:
            The same list with optional enrichment fields added.
        """
        all_syms = [item["symbol"] for item in candidates]
        top_syms = all_syms[:30]
        _TIMEOUT = 20

        pool   = _cf.ThreadPoolExecutor(max_workers=5)
        f_opt  = pool.submit(self.options_flow.get_options_flow, top_syms)
        f_dp   = pool.submit(self.dark_pool.get_dark_pool_signals, all_syms)
        f_pm   = pool.submit(self.pre_market.get_premarket_data,   all_syms)
        f_news = pool.submit(self.broker.get_news_headlines, top_syms, 4)
        _cf.wait([f_opt, f_dp, f_pm, f_news], timeout=_TIMEOUT)
        for _f in (f_opt, f_dp, f_pm, f_news):
            _f.cancel()
        pool.shutdown(wait=False)

        def _safe(fut):
            try:
                return fut.result(timeout=0) if fut.done() else {}
            except Exception:
                return {}

        options_data = _safe(f_opt)
        dp_data      = _safe(f_dp)
        pm_data      = _safe(f_pm)
        news_data    = _safe(f_news)

        for item in candidates:
            sym = item["symbol"]
            if sym in options_data:
                item["options_flow"] = options_data[sym]
                # Carry unusual call/put signal into the candidate for IV selector
                item["high_conviction_flow"] = options_data[sym].get("high_conviction", False)
            if sym in dp_data:
                item["dark_pool"] = dp_data[sym]
            if sym in pm_data:
                item["pre_market"] = pm_data[sym]
            if isinstance(news_data, dict) and sym in news_data:
                headlines = [h["headline"] for h in (news_data.get(sym) or [])[:5]]
                cat_score, _ = _score_catalyst(headlines)
                item["has_catalyst"]   = cat_score > 0
                item["catalyst_score"] = cat_score

        done = sum(1 for f in [f_opt, f_dp, f_pm, f_news] if f.done())
        if done < 4:
            log.warning("Enrichment: %d/4 calls finished in %ds", done, _TIMEOUT)

        return candidates

    # ── SPY move ──────────────────────────────────────────────────────────────

    def _compute_spy_move_pct(self) -> float:
        """
        Compute SPY's percent move from today's open.

        Used by the 0DTE engine to determine direction for momentum spreads.

        Returns:
            SPY move as percent (positive = up, negative = down). Returns 0.0 on failure.
        """
        try:
            bars = self.broker.get_bars("SPY", "5Min", days=1)
            if bars is None or bars.empty:
                return 0.0
            spy_open  = float(bars["open"].iloc[0])
            spy_close = float(bars["close"].iloc[-1])
            if spy_open <= 0:
                return 0.0
            return round((spy_close - spy_open) / spy_open * 100, 3)
        except Exception:
            return 0.0
