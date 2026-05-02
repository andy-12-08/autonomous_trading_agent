import time as _time
import config
from core.database import log


class ScannerMixin:
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
            regime: Intaday regime label passed to the signal scorer.

        Returns:
            List of candidate dicts that passed the signal filter, best score first.
        """
        scan_list = universe if universe is not None else config.WATCHLIST
        plan_candidates = {
            c["symbol"] for c in (daily_plan or {}).get("top_candidates", [])
        }

        log.info("Fetching bars for %d symbols across 3 timeframes…", len(scan_list))
        bars_5m  = self.broker.get_bars_multi(scan_list, "5Min",  days=3)
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

        raw = []
        _t_loop_start = _time.monotonic()
        for symbol in scan_list:
            _t_sym = _time.monotonic()
            try:
                df = bars_5m.get(symbol)
                if df is None or df.empty or len(df) < 25:
                    continue
                _t0 = _time.monotonic()
                df  = self.indicators.compute_indicators(df)
                _dt_ind = _time.monotonic() - _t0

                sig = self.indicators.get_signal_summary(df)
                if not sig:
                    continue

                if self.risk_manager.is_too_volatile(sig.get("atr", 0), sig.get("price", 1)):
                    log.info("Skip %s — ATR too high (%.1f%%)",
                             symbol, sig.get("atr", 0) / sig.get("price", 1) * 100)
                    continue

                sym_price = sig.get("price", 0)
                if sym_price < config.SCREENER_MIN_PRICE:
                    log.info("Skip %s — price too low ($%.2f)", symbol, sym_price)
                    continue

                atr_pct   = sig.get("atr", 0) / max(sym_price, 0.01)
                vol_ratio = sig.get("vol_ratio", 0)
                trend     = sig.get("trend", "neutral")
                above_vwap = sig.get("above_vwap", False)
                if atr_pct < 0.004 and vol_ratio < 0.6 and trend == "neutral" and not above_vwap:
                    continue

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

                key_levels = self.indicators.get_key_levels(df, df_day)
                self._key_levels_cache[symbol] = key_levels

                _dt_sym = _time.monotonic() - _t_sym
                if _dt_sym > 0.5:
                    log.warning("SLOW symbol %s: total=%.2fs ind=%.2fs htf=%.2fs",
                                symbol, _dt_sym, _dt_ind, _dt_htf)

                raw.append({
                    "symbol":     symbol,
                    "bucket":     config.SYMBOL_BUCKET.get(symbol, "unknown"),
                    "in_plan":    symbol in plan_candidates,
                    "indicators": sig,
                    "bias_15min": bias_15,
                    "bias_daily": bias_day,
                    "key_levels": key_levels,
                })
            except Exception as e:
                log.warning("Watchlist error %s: %s", symbol, e)

        log.info("Symbol loop done: %d candidates in %.1fs", len(raw), _time.monotonic() - _t_loop_start)
        scored = self.signal_scorer.filter_watchlist(
            raw, midday=midday, regime=regime, session_overrides=self.session_overrides
        )
        log.info("Watchlist: %d/%d symbols passed signal filter (midday=%s)",
                 len(scored), len(raw), midday)
        for item in scored[:6]:  # log top 6
            b15  = item.get("bias_15min") or {}
            bday = item.get("bias_daily") or {}
            bull15  = sum([bool(b15.get("ema_bull")),  bool(b15.get("above_vwap")),  bool(b15.get("macd_bull"))])
            bullday = sum([bool(bday.get("ema_bull")), bool(bday.get("above_vwap")), bool(bday.get("ema50_bull") or False)])
            log.info("  %-6s score=%.1f [%s] 15m=%d/3 day=%d/3 | %s",
                     item["symbol"], item["signal_score"], item["signal_class"],
                     bull15, bullday,
                     " | ".join(item["signal_evidence"][:3]))
        return scored

