import concurrent.futures as _cf
import time as _time
import config
from core.database import log

_POOL_WORKERS = 6   # max concurrent symbol computations
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

        log.info("Fetching bars for %d symbols across 3 timeframes…", len(scan_list))
        bars_5m  = self.broker.get_bars_multi(scan_list, "5Min",  days=10)
        bars_15m = self.broker.get_bars_multi(scan_list, "15Min", days=5)
        bars_day = self.broker.get_bars_multi(scan_list, "1Day",  days=30)
        log.info("Bars received — 5m:%d  15m:%d  daily:%d symbols",
                 len(bars_5m), len(bars_15m), len(bars_day))

        spy_5m = bars_5m.get("SPY")
        if spy_5m is None or spy_5m.empty:
            try:
                spy_5m = self.broker.get_bars("SPY", "5Min", days=1)
            except Exception:
                spy_5m = None

        # SPY trend gate: are the last 3 five-minute bars net positive?
        # Used in the executor to block long entries when the market is falling.
        if spy_5m is not None and len(spy_5m) >= 4:
            _spy_c = spy_5m["close"].iloc[-4:].values
            self._spy_trend_ok = bool(_spy_c[-1] > _spy_c[-4])  # last bar above 3 bars ago
            log.info("SPY trend: %s (close %.2f vs %.2f, 3 bars ago)",
                     "UP" if self._spy_trend_ok else "DOWN", _spy_c[-1], _spy_c[-4])
        else:
            self._spy_trend_ok = True  # can't determine — don't block

        raw = []
        _t_loop_start = _time.monotonic()

        def _compute_symbol(symbol):
            df = bars_5m.get(symbol)
            if df is None or df.empty or len(df) < 25:
                return None
            _t0 = _time.monotonic()
            df  = self.indicators.compute_indicators(df)
            _dt_ind = _time.monotonic() - _t0

            sig = self.indicators.get_signal_summary(df)
            if not sig:
                return None

            if self.risk_manager.is_too_volatile(sig.get("atr", 0), sig.get("price", 1)):
                log.info("Skip %s — ATR too high (%.1f%%)",
                         symbol, sig.get("atr", 0) / sig.get("price", 1) * 100)
                return None

            sym_price = sig.get("price", 0)
            if sym_price < config.SCREENER_MIN_PRICE:
                log.info("Skip %s — price too low ($%.2f)", symbol, sym_price)
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

            _t0 = _time.monotonic()
            df_15  = bars_15m.get(symbol)
            df_day = bars_day.get(symbol)
            bias_15  = self.indicators.get_higher_tf_bias(df_15)
            bias_day = self.indicators.get_higher_tf_bias(df_day)
            _dt_htf = _time.monotonic() - _t0

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

            # Float lookup — fast SQLite read (7-day cache); None when symbol not yet cached
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

            key_levels = self.indicators.get_key_levels(df, df_day)
            self._key_levels_cache[symbol] = key_levels

            _dt_sym = _time.monotonic() - _t0 + _dt_ind + _dt_htf
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
            log.warning("Symbol loop budget exhausted (>%ds) — %d results, %d abandoned",
                        _LOOP_BUDGET, len(raw), pending)
        pool.shutdown(wait=False, cancel_futures=True)

        log.info("Symbol loop done: %d candidates in %.1fs", len(raw), _time.monotonic() - _t_loop_start)
        scored = self.signal_scorer.filter_watchlist(
            raw, midday=midday, regime=regime, session_overrides=self.session_overrides
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

