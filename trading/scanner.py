import concurrent.futures as _cf
import time as _time
import config
from core.database import log

_POOL_WORKERS = 12  # max concurrent symbol computations
_LOOP_BUDGET  = 90  # total wall-clock seconds for symbol loop


class ScannerMixin:
    """Build scored watchlist rows from cached bars and the signal scorer."""

    def build_watchlist_data(
        self,
        daily_plan: dict | None,
        midday: bool = False,
        universe: list[str] | None = None,
        regime: str = "ranging",
    ) -> list[dict]:
        """Fetch bars, compute indicators, score setups; return passing candidates sorted by score.

        Args:
            daily_plan: Optional morning plan (e.g. top_candidates).
            midday: If True, applies stricter midday thresholds in the scorer.
            universe: Symbols to scan; defaults to config.WATCHLIST.
            regime: Intraday regime label passed to the signal scorer.

        Returns:
            List of candidate dicts that passed the signal filter, best score first.
        """
        scan_list = universe if universe is not None else config.WATCHLIST
        plan_candidates = {
            c["symbol"] for c in (daily_plan or {}).get("top_candidates", [])
        }

        log.info("Fetching 5m bars for %d symbols", len(scan_list))
        bars_5m = self.broker.get_bars_multi(
            scan_list, "5Min", days=getattr(config, "SCAN_5M_HISTORY_DAYS", 5)
        )
        log.info("Bars received  5m:%d symbols", len(bars_5m))

        spy_5m = bars_5m.get("SPY")
        if spy_5m is None or spy_5m.empty:
            try:
                spy_5m = self.broker.get_bars("SPY", "5Min", days=1)
            except Exception:
                spy_5m = None

        # SPY trend gate: block longs only if SPY is down meaningfully over last 15 min.
        # Threshold of 0.15% filters out normal oscillations; only real selling triggers it.
        if spy_5m is not None and len(spy_5m) >= 4:
            _spy_c = spy_5m["close"].iloc[-4:].values
            _spy_chg_pct = (_spy_c[-1] - _spy_c[-4]) / _spy_c[-4] * 100
            self._spy_trend_ok = _spy_chg_pct > -0.15
            if self._spy_trend_ok:
                self._spy_trend_ok_streak = getattr(self, "_spy_trend_ok_streak", 0) + 1
            else:
                self._spy_trend_ok_streak = 0
            log.info("SPY trend: %s (close %.2f vs %.2f, 3 bars ago, chg=%.3f%%) streak=%d",
                     "UP" if self._spy_trend_ok else "DOWN", _spy_c[-1], _spy_c[-4],
                     _spy_chg_pct, self._spy_trend_ok_streak)
        else:
            self._spy_trend_ok = True  # can't determine  don't block
            self._spy_trend_ok_streak = getattr(self, "_spy_trend_ok_streak", 0) + 1

        raw = []
        _t_loop_start = _time.monotonic()

        def _compute_symbol(symbol):
            """Build a scored watchlist row for one symbol.

            Args:
                symbol: Ticker to evaluate using cached 5-minute bars.

            Returns:
                Watchlist row dict, or None when the symbol fails screening.
            """
            df = bars_5m.get(symbol)
            if df is None or df.empty or len(df) < 25:
                return None
            _t_sym_start = _time.monotonic()
            _t0 = _time.monotonic()
            df  = self.indicators.compute_indicators(df)
            _dt_ind = _time.monotonic() - _t0

            sig = self.indicators.get_signal_summary(df)
            if not sig:
                return None

            if self.risk_manager.is_too_volatile(sig.get("atr", 0), sig.get("price", 1)):
                log.info("Skip %s  ATR too high (%.1f%%)",
                         symbol, sig.get("atr", 0) / sig.get("price", 1) * 100)
                return None

            sym_price = sig.get("price", 0)
            if sym_price < config.SCREENER_MIN_PRICE:
                log.info("Skip %s  price too low ($%.2f)", symbol, sym_price)
                return None

            atr_pct    = sig.get("atr", 0) / max(sym_price, 0.01)
            vol_ratio  = sig.get("vol_ratio", 0)
            trend      = sig.get("trend", "neutral")
            above_vwap = sig.get("above_vwap", False)
            if atr_pct < 0.004 and vol_ratio < 0.6 and trend == "neutral" and not above_vwap:
                return None

            if spy_5m is not None and symbol != "SPY":
                rs = self.indicators.compute_relative_strength(df, spy_5m)
                if rs is not None:
                    sig["rs_vs_spy"] = rs

            sig.update(self.indicators.compute_premium_discount(df))
            sig.update(self.indicators.detect_fvg(df))
            sig.update(self.indicators.detect_liquidity_sweep(df, key_levels=self._key_levels_cache.get(symbol)))
            sig.update(self.indicators.compute_volume_profile(df))

            bias_15 = {}
            bias_day = {}
            _dt_htf = 0.0

            # True time-slot RVOL for day trading.
            # Compares today's cumulative volume through bar N to the average
            # cumulative volume through bar N across prior sessions in the 5-min
            # history.  This correctly models the U-shaped intraday volume curve
            # (opening heavy, midday light) without any linear-time-adjustment math.
            # Requires days=10 so we have ~8 prior sessions to average over.
            try:
                _idx_et = df.index.tz_convert(config.ET) if df.index.tz else df.index
                _today  = _idx_et[-1].date()
                _today_mask = [t.date() == _today for t in _idx_et]
                _prior_mask = [t.date() <  _today for t in _idx_et]
                _df_today   = df[_today_mask]
                _df_prior   = df[_prior_mask]
                n_bars      = len(_df_today)   # bars completed so far today
                if n_bars >= 1 and not _df_prior.empty:
                    _prior_dates = sorted({t.date() for t in _idx_et[_prior_mask]})
                    _prior_cumvols = []
                    for _d in _prior_dates:
                        _day_vols = _df_prior[[t.date() == _d for t in _idx_et[_prior_mask]]]["volume"]
                        if len(_day_vols) >= n_bars:
                            _prior_cumvols.append(float(_day_vols.iloc[:n_bars].sum()))
                    _today_cumvol = float(_df_today["volume"].sum())
                    if _prior_cumvols:
                        _avg_prior = sum(_prior_cumvols) / len(_prior_cumvols)
                        if _avg_prior > 0:
                            sig["rvol"] = round(min(_today_cumvol / _avg_prior, 20.0), 2)
            except Exception:
                pass  # fall back to vol_ratio computed by compute_indicators

            # Float lookup  fast SQLite read (7-day cache); None when symbol not yet cached
            if hasattr(self, "float_cache") and self.float_cache is not None:
                float_shares = self.float_cache.get_float_cached(symbol)
                if float_shares is not None:
                    sig["float_shares"] = float_shares
                    if float_shares < 5_000_000:
                        sig["float_tier"] = "micro"
                    elif float_shares < 20_000_000:
                        sig["float_tier"] = "small"
                    elif float_shares < 100_000_000:
                        sig["float_tier"] = "mid"
                    else:
                        sig["float_tier"] = "large"

            key_levels = self.indicators.get_key_levels(df, None)
            self._key_levels_cache[symbol] = key_levels

            _dt_sym = _time.monotonic() - _t_sym_start
            if _dt_sym > 0.5:
                log.warning("SLOW symbol %s: total=%.2fs ind=%.2fs htf=%.2fs",
                            symbol, _dt_sym, _dt_ind, _dt_htf)

            return {
                "symbol":     symbol,
                "bucket":     config.SYMBOL_BUCKET.get(symbol, "unknown"),
                "in_plan":    symbol in plan_candidates,
                "indicators": sig,
                "bias_15min": bias_15,
                "bias_daily": bias_day,
                "key_levels": key_levels,
            }

        pool    = _cf.ThreadPoolExecutor(max_workers=_POOL_WORKERS)
        futures = {pool.submit(_compute_symbol, sym): sym for sym in scan_list}
        try:
            for fut in _cf.as_completed(futures, timeout=_LOOP_BUDGET):
                sym = futures[fut]
                try:
                    result = fut.result(timeout=0)
                    if result is not None:
                        raw.append(result)
                except Exception as exc:
                    log.warning("Watchlist error %s: %s", sym, exc)
        except _cf.TimeoutError:
            pending = sum(1 for f in futures if not f.done())
            log.warning("Symbol loop budget exhausted (>%ds)  %d results, %d abandoned",
                        _LOOP_BUDGET, len(raw), pending)
        pool.shutdown(wait=False, cancel_futures=True)

        log.info("Symbol loop done: %d candidates in %.1fs", len(raw), _time.monotonic() - _t_loop_start)
        prelim_min = (
            self.session_overrides.get("signal_score_min_midday")
            if midday else
            self.session_overrides.get("signal_score_min_normal")
        ) if self.session_overrides is not None else (
            config.MIDDAY_MIN_SIGNAL_SCORE if midday else config.NORMAL_MIN_SIGNAL_SCORE
        )
        prelim_floor = max(3.5, float(prelim_min) - 2.5)
        prelim = []
        for item in raw:
            sig = item.get("indicators", {})
            mom_score, mom_ev = self.signal_scorer.score_setup(sig, {}, {})
            gap_score, gap_ev = self.signal_scorer.score_gap_and_go(sig)
            vwap_score, vwap_ev = self.signal_scorer.score_vwap_reclaim(sig)
            best_score, best_ev, best_type = max(
                [
                    (gap_score, gap_ev, "gap_and_go"),
                    (vwap_score, vwap_ev, "vwap_reclaim"),
                    (mom_score, mom_ev, "momentum"),
                ],
                key=lambda x: x[0],
            )
            if best_score >= prelim_floor:
                row = dict(item)
                row["prelim_score"] = best_score
                row["prelim_setup_type"] = best_type
                row["prelim_evidence"] = best_ev[:4]
                prelim.append(row)
        prelim.sort(key=lambda x: x["prelim_score"], reverse=True)

        htf_limit = int(getattr(config, "SCAN_STAGE1_CANDIDATE_LIMIT", 40))
        htf_candidates = prelim[:htf_limit]
        htf_symbols = [item["symbol"] for item in htf_candidates]
        log.info(
            "Prelim candidates: %d/%d >= %.1f; enriching top %d with 15m/daily",
            len(prelim), len(raw), prelim_floor, len(htf_symbols),
        )

        bars_15m = {}
        bars_day = {}
        if htf_symbols:
            with _cf.ThreadPoolExecutor(max_workers=2) as htf_pool:
                fut_15m = htf_pool.submit(self.broker.get_bars_multi, htf_symbols, "15Min", 5)
                fut_day = htf_pool.submit(self.broker.get_bars_multi, htf_symbols, "1Day", 30)
                try:
                    bars_15m = fut_15m.result(timeout=getattr(config, "BARS_MULTI_TIMEOUT_SECONDS", 25) + 5)
                except Exception as exc:
                    log.warning("15m enrichment fetch failed/timed out: %s", exc)
                    fut_15m.cancel()
                try:
                    bars_day = fut_day.result(timeout=getattr(config, "BARS_MULTI_TIMEOUT_SECONDS", 25) + 5)
                except Exception as exc:
                    log.warning("daily enrichment fetch failed/timed out: %s", exc)
                    fut_day.cancel()
        log.info("Bars received  15m:%d  daily:%d symbols", len(bars_15m), len(bars_day))

        for item in htf_candidates:
            symbol = item["symbol"]
            df_15 = bars_15m.get(symbol)
            df_day = bars_day.get(symbol)
            item["bias_15min"] = self.indicators.get_higher_tf_bias(df_15)
            item["bias_daily"] = self.indicators.get_higher_tf_bias(df_day)
            key_levels = self.indicators.get_key_levels(bars_5m.get(symbol), df_day)
            if key_levels:
                item["key_levels"] = key_levels
                self._key_levels_cache[symbol] = key_levels

        scored = self.signal_scorer.filter_watchlist(
            htf_candidates, midday=midday, regime=regime, session_overrides=self.session_overrides
        )
        log.info("Watchlist: %d/%d symbols passed signal filter (midday=%s)",
                 len(scored), len(raw), midday)
        for item in scored[:6]:
            b15  = item.get("bias_15min") or {}
            bday = item.get("bias_daily") or {}
            bull15  = sum([bool(b15.get("ema_bull")),  bool(b15.get("above_vwap")),  bool(b15.get("macd_bull"))])
            bullday = sum([bool(bday.get("ema_bull")), bool(bday.get("above_vwap")), bool(bday.get("ema50_bull") or False)])
            log.info("  %-6s score=%.1f [%s] 15m=%d/3 day=%d/3 | %s",
                     item["symbol"], item["signal_score"], item["signal_class"],
                     bull15, bullday,
                     " | ".join(item["signal_evidence"][:3]))
        return scored
