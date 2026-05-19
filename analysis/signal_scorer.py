from datetime import datetime

import config

from core.database import log
from analysis.signal_rules import SignalRulesMixin


class SignalScorer(SignalRulesMixin):
    """Score indicator snapshots and filter watchlists by setup quality."""

    @staticmethod
    def score_setup(sig: dict,
                    bias_15: dict | None = None,
                    bias_day: dict | None = None) -> tuple[float, list[str]]:
        """
        Score a momentum/structural setup across seven independent dimensions.

        Scoring dimensions and their maximum raw contribution:
          Trend         3.0 pts   EMA stack, VWAP position
          Momentum      2.5 pts   MACD histogram, crossover, 10-bar momentum
          Volume        2.0 pts   vol_ratio vs 20-bar average
          ATR           1.5 pts   volatility in the tradeable sweet spot
          RSI           1.0 pt    sweet-spot bonus; overbought/oversold penalties
          Multi-TF      3.0 pts   15-min + daily bias alignment
          Structural   =5.0 pts   RS vs SPY, premium/discount, FVG, volume profile

        Penalties are applied last and can reduce the score significantly.
        Final score is clamped to [0.0, 10.0].

        Args:
            sig:      Flat indicator dict from indicators.get_signal_summary(),
                      extended with structural fields (rs_vs_spy, range_pct,
                      near_bull_fvg, poc, vah, val, lvn_above, etc.).
            bias_15:  Higher-TF bias dict from indicators.get_higher_tf_bias()
                      on 15-min bars. None = no 15-min confirmation available.
            bias_day: Higher-TF bias dict from indicators.get_higher_tf_bias()
                      on daily bars. None = no daily confirmation available.

        Returns:
            Tuple of (score, evidence_list) where score is a float in [0.0, 10.0]
            and evidence_list contains one human-readable string per scoring event.
        """
        score   = 0.0
        ev      = []

        price     = float(sig.get("price",    0))
        ema9      = float(sig.get("ema9",     0))
        ema21     = float(sig.get("ema21",    0))
        ema50     = float(sig.get("ema50",    0))
        macd_hist = float(sig.get("macd_hist", 0))
        macd_x    = sig.get("macd_cross",    "neutral")
        rsi       = float(sig.get("rsi",      50))
        vol_ratio = float(sig.get("vol_ratio", 1.0))
        atr       = float(sig.get("atr",      0))
        above_vwap = bool(sig.get("above_vwap", False))
        mom10     = float(sig.get("mom10",    0))

        atr_pct = atr / price if price > 0 else 0

        # -- TREND (3.0 pts) --------------------------------------------------------
        if ema9 > ema21:
            score += 1.0; ev.append("+1.0 EMA9>EMA21")
        if above_vwap:
            score += 1.0; ev.append("+1.0 above VWAP")
        if ema50 > 0 and price > ema50:
            score += 0.5; ev.append("+0.5 price>EMA50")
        if ema9 > ema21 > ema50 > 0:
            score += 0.5; ev.append("+0.5 full EMA bull stack")

        # -- EMA EXTENSION PENALTY -------------------------------------------------
        # Price too far above EMA21 = chasing a move already done; pullback risk high.
        # Penalty scales with extension: 1.5% above is -0.6 pts, 3% above is -1.2 pts, cap -2.0.
        if ema21 > 0 and price > 0:
            ext = (price - ema21) / ema21
            if ext > 0.015:
                penalty = round(min(ext * 40, 2.0), 1)
                score  -= penalty
                ev.append(f"-{penalty} extended {ext:.1%} above EMA21  pullback risk")

        # -- MOMENTUM  MACD (2.5 pts) ----------------------------------------------
        if macd_x == "bullish":
            score += 2.0; ev.append("+2.0 MACD bullish cross")
        elif macd_hist > 0 and macd_x != "bearish":
            score += 0.8; ev.append("+0.8 MACD hist positive")
        if 0.3 <= mom10 <= 4.0:
            score += 0.5; ev.append(f"+0.5 10-bar momentum {mom10:.1f}%")

        # -- VOLUME / RVOL (2.0 pts) ------------------------------------------------
        # Prefer rvol (daily-bar-based) when available; fall back to vol_ratio.
        rvol = float(sig.get("rvol") or sig.get("vol_ratio") or 1.0)
        if rvol >= 3.0:
            score += 2.0; ev.append(f"+2.0 RVOL surge {rvol:.1f}x avg daily volume")
        elif rvol >= 2.0:
            score += 1.5; ev.append(f"+1.5 RVOL elevated {rvol:.1f}x")
        elif rvol >= 1.3:
            score += 0.8; ev.append(f"+0.8 RVOL above average {rvol:.1f}x")
        elif rvol >= 1.0:
            score += 0.3; ev.append(f"+0.3 RVOL on pace {rvol:.1f}x")

        # Float-tier bonus: low-float + surge volume = explosive move potential
        float_tier = sig.get("float_tier", "unknown")
        if float_tier in ("micro", "small") and rvol >= 2.0:
            score += 0.5; ev.append(f"+0.5 low-float ({float_tier}) + surge RVOL  momentum amplifier")

        # -- ATR / VOLATILITY (1.5 pts) ---------------------------------------------
        # Ideal range: 0.5%2.0% ATR/price  enough movement but not chaos
        if 0.005 <= atr_pct <= 0.020:
            score += 1.5; ev.append(f"+1.5 ATR ideal {atr_pct:.1%}")
        elif 0.002 <= atr_pct < 0.005:
            score += 0.5; ev.append(f"+0.5 ATR low-normal {atr_pct:.1%}")
        elif 0.020 < atr_pct <= 0.035:
            score += 0.5; ev.append(f"+0.5 ATR elevated {atr_pct:.1%}")
        # > 3.5% ATR: no bonus  too chaotic

        # -- RSI POSITION (1.0 pt) -------------------------------------------------
        if 42 <= rsi <= 60:
            score += 1.0; ev.append(f"+1.0 RSI sweet spot {rsi:.0f}")
        elif 35 <= rsi < 42:
            score += 0.5; ev.append(f"+0.5 RSI recovering {rsi:.0f}")

        # -- MULTI-TIMEFRAME CONFIRMATION (3.0 pts max) ----------------------------
        # 15-min bias: does the broader intraday trend agree with the 5-min signal?
        # This is the single most important filter  a 5-min MACD cross that
        # contradicts the 15-min trend is almost always noise.
        if bias_15:
            bull15 = sum([
                bool(bias_15.get("ema_bull")),
                bool(bias_15.get("above_vwap")),
                bool(bias_15.get("macd_bull")),
            ])
            if bull15 == 3:
                score += 2.0; ev.append("+2.0 15-min fully aligned (EMA+VWAP+MACD bullish)")
            elif bull15 == 2:
                score += 0.8; ev.append(f"+0.8 15-min partially bullish ({bull15}/3)")
            elif bull15 == 1:
                score -= 1.0; ev.append(f"-1.0 15-min weak ({bull15}/3)  5-min signal questionable")
            else:
                score -= 2.0; ev.append("-2.0 15-min fully bearish  contradicts 5-min signal")

        # Daily bias: is the stock in a broader uptrend this week?
        # Worth less than 15-min  a down-day can still have great intraday setups.
        if bias_day:
            rsi_day  = float(bias_day.get("rsi") or 50)
            bull_day = sum([
                bool(bias_day.get("ema_bull")),
                bool(bias_day.get("above_vwap")),
                bool(bias_day.get("ema50_bull") or False),
            ])
            if bull_day >= 2 and rsi_day < 68:
                score += 1.0; ev.append(f"+1.0 daily bias bullish ({bull_day}/3, RSI {rsi_day:.0f})")
            elif bull_day == 0:
                score -= 0.5; ev.append(f"-0.5 daily bias bearish ({bull_day}/3)")

        # -- STRUCTURAL ANALYSIS ---------------------------------------------------
        # Relative strength vs SPY
        rs_raw = sig.get("rs_vs_spy")
        if rs_raw is not None:
            rs = float(rs_raw)
            if rs >= 2.0:
                score += 2.0; ev.append(f"+2.0 RS {rs:.1f}x vs SPY  institutional accumulation")
            elif rs >= 1.3:
                score += 1.0; ev.append(f"+1.0 outperforming SPY {rs:.1f}x")
            elif rs >= 0.8:
                score += 0.3; ev.append(f"+0.3 slight outperformance vs SPY {rs:.1f}x")
            elif rs < 0:
                score -= 1.5; ev.append(f"-1.5 negative RS {rs:.1f}x  falling while SPY rises")
            elif rs < 0.5:
                score -= 0.8; ev.append(f"-0.8 weak RS {rs:.1f}x  lagging market")

        # Premium / discount zone (Fibonacci 50% rule)
        range_pct_raw = sig.get("range_pct")
        if range_pct_raw is not None:
            rp       = float(range_pct_raw)
            ema_bull = ema9 > ema21
            if rp <= 35 and ema_bull and above_vwap:
                score += 0.8; ev.append(f"+0.8 discount zone {rp:.0f}%  pullback in uptrend")
            elif rp >= 80 and not ema_bull:
                score -= 1.0; ev.append(f"-1.0 premium zone {rp:.0f}%  extended with bearish EMA")
            elif rp >= 90:
                score -= 0.5; ev.append(f"-0.5 deep premium {rp:.0f}%  very extended")

        # Fair Value Gaps
        if bool(sig.get("near_bull_fvg")):
            score += 1.0; ev.append("+1.0 at bullish FVG support  institutional order zone")
        if bool(sig.get("near_bear_fvg")):
            score -= 0.5; ev.append("-0.5 approaching bearish FVG resistance")

        # Volume Profile  POC / LVN
        poc_raw = sig.get("poc")
        if poc_raw is not None:
            poc = float(poc_raw)
            vah_val = sig.get("vah") or 0.0
            val_val = sig.get("val") or 0.0
            if sig.get("above_value_area") and ema9 > ema21 and above_vwap:
                score += 1.0; ev.append(f"+1.0 above VAH {float(vah_val):.2f}  bullish expansion out of balance")
            elif sig.get("in_value_area"):
                score -= 0.5; ev.append(f"-0.5 inside value area  auction zone, momentum setups stall here")
            elif sig.get("below_value_area") and val_val > 0:
                score -= 0.5; ev.append(
                    f"-0.5 below value area ({float(val_val):.2f})  rejected from volume support")
            if sig.get("near_poc"):
                score += 0.8; ev.append(f"+0.8 near POC {poc:.2f}  max-volume magnet, high-prob reaction")
            lvn_raw = sig.get("lvn_above")
            if lvn_raw is not None and price > 0:
                lvn_dist_pct = (float(lvn_raw) - price) / price * 100
                if 0.4 <= lvn_dist_pct <= 3.0:
                    score += 0.5; ev.append(
                        f"+0.5 LVN at {float(lvn_raw):.2f} ({lvn_dist_pct:.1f}% up)  free air, fast move to TP")

        # Liquidity sweep (institutional stop hunt + reversal  one of the highest-probability setups)
        # Price pierces a key level, hunts retail stops, then closes back above on volume.
        # Fresh sweep (entry within 0.3% of current price) is scored higher  signal is still hot.
        if sig.get("liquidity_sweep_detected"):
            sweep_entry = sig.get("sweep_entry")
            if sweep_entry and price > 0 and abs(price - float(sweep_entry)) / price <= 0.003:
                score += 1.5; ev.append(f"+1.5 fresh liquidity sweep at {float(sweep_entry):.2f}  institutional entry")
            else:
                score += 0.8; ev.append("+0.8 liquidity sweep detected  stop hunt reversal")

        # -- PENALTIES -------------------------------------------------------------
        if rsi > 70:
            score -= 2.5; ev.append(f"-2.5 RSI overbought {rsi:.0f}")
        elif rsi > 65:
            score -= 1.0; ev.append(f"-1.0 RSI getting hot {rsi:.0f}")
        if rsi < 30:
            score -= 1.5; ev.append(f"-1.5 RSI deeply oversold {rsi:.0f}")
        if rvol < 0.7:
            score -= 2.0; ev.append(f"-2.0 RVOL very low {rvol:.1f}x (illiquid)")
        elif rvol < 1.0:
            score -= 0.8; ev.append(f"-0.8 RVOL below average {rvol:.1f}x")
        if macd_x == "bearish":
            score -= 2.0; ev.append("-2.0 MACD bearish cross")
        if atr_pct > 0.04:
            score -= 1.5; ev.append(f"-1.5 ATR dangerously high {atr_pct:.1%}")
        if mom10 < -2.0:
            score -= 1.0; ev.append(f"-1.0 negative momentum {mom10:.1f}%")
        if not above_vwap and ema9 < ema21:
            score -= 0.8; ev.append("-0.8 below VWAP + bearish EMA")

        # -- TIME-OF-DAY ADJUSTMENT ------------------------------------------------
        # 9:35-10:00: opening range fakeout risk high, so apply a slight penalty.
        # 10:00-11:30: institutional settlement; momentum is most reliable.
        # 11:30-14:00: lunch doldrums make momentum less reliable.
        # 14:00-15:44: power hour trend continuation is active.
        _et_now  = datetime.now(config.ET)
        _cur_min = _et_now.hour * 60 + _et_now.minute
        if 9 * 60 + 35 <= _cur_min < 10 * 60:
            score -= 0.3; ev.append("-0.3 opening range (fakeout risk)")
        elif 11 * 60 + 30 <= _cur_min < 14 * 60:
            score -= 0.5; ev.append("-0.5 lunch doldrums (momentum setups weaker)")
        elif 14 * 60 <= _cur_min <= 15 * 60 + 44:
            score += 0.3; ev.append("+0.3 power hour (trend continuation bias)")

        score = round(max(0.0, min(10.0, score)), 1)
        return score, ev

    @staticmethod
    def classify(score: float) -> str:
        """
        Map a numeric score to a human-readable quality tier.

        Args:
            score: Numeric signal quality score in [0.0, 10.0].

        Returns:
            "STRONG" (=7.5), "GOOD" (=6.5), "WEAK" (=5.0), or "SKIP" (<5.0).
        """
        if score >= 7.5:
            return "STRONG"
        if score >= 6.5:
            return "GOOD"
        if score >= 5.0:
            return "WEAK"
        return "SKIP"

    @staticmethod
    def filter_watchlist(watchlist_data: list[dict],
                         midday: bool = False,
                         regime: str = "ranging",
                         session_overrides=None) -> list[dict]:
        """
        Score every symbol across all four setup types and drop anything below
        the quality threshold.

        Scorers run in parallel; the highest valid score wins:
          gap_and_go     ORB breakout with gap (9:3511:00 ET only)
          vwap_reclaim   mean-reversion: price reclaims VWAP from below
          mean_reversion  price below POC inside value area, reverting to mean
          momentum       EMA/MACD/volume trend-following (always active)

        The minimum qualifying score is read from session_overrides when provided,
        or falls back to config constants. The regime parameter provides an
        additional real-time adjustment: choppy markets raise the bar by 1 point
        so only the highest-conviction setups survive.

        Args:
            watchlist_data:   List of raw symbol dicts with "indicators",
                              "bias_15min", and "bias_daily" keys.
            midday:           True during 11 AM2 PM; applies higher midday threshold.
            regime:           "trending" | "ranging" | "choppy"  from intraday
                              regime detector. "choppy" raises min_score by 1.0.
            session_overrides: Optional session overrides object with a .get() method.
                               When provided, thresholds are read from it instead of
                               config constants.

        Returns:
            Filtered and scored list, sorted by signal_score descending.
            Each item gains: signal_score, signal_class, signal_evidence,
            setup_type_hint.
        """
        if session_overrides is not None:
            min_score = session_overrides.get("signal_score_min_midday") if midday else session_overrides.get("signal_score_min_normal")
        else:
            min_score = config.MIDDAY_MIN_SIGNAL_SCORE if midday else config.NORMAL_MIN_SIGNAL_SCORE

        # Choppy regime: do NOT raise the threshold  choppy markets are where
        # mean-reversion and VWAP-reclaim setups thrive. The scorers already
        # penalise momentum/trend setups in choppy conditions. Claude gets the
        # regime context and applies its own discretion on position sizing.
        log.info(
            "Signal filter: min_score=%.1f regime=%s %s",
            min_score, regime, "(midday)" if midday else "(high-vol)"
        )

        scored = []
        for item in watchlist_data:
            sig      = item.get("indicators", {})
            bias_15  = item.get("bias_15min") or {}
            bias_day = item.get("bias_daily") or {}

            mom_score,  mom_ev  = SignalScorer.score_setup(sig, bias_15, bias_day)
            gap_score,  gap_ev  = SignalScorer.score_gap_and_go(sig)
            vwap_score, vwap_ev = SignalScorer.score_vwap_reclaim(sig)
            mr_score,   mr_ev   = SignalScorer.score_mean_reversion(sig)

            # Pick the strongest valid setup this bar
            candidates = [
                (gap_score,  gap_ev,  "gap_and_go"),
                (vwap_score, vwap_ev, "vwap_reclaim"),
                (mr_score,   mr_ev,   "mean_reversion"),
                (mom_score,  mom_ev,  "momentum"),
            ]
            best_score, best_ev, best_type = max(candidates, key=lambda x: x[0])

            item = dict(item)
            item["signal_score"]    = best_score
            item["signal_class"]    = SignalScorer.classify(best_score)
            item["signal_evidence"] = best_ev[:8]
            item["setup_type_hint"] = best_type

            if best_score >= min_score:
                scored.append(item)

        scored.sort(key=lambda x: x["signal_score"], reverse=True)
        return scored
