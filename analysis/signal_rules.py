from datetime import datetime

import config
from core.database import log


class SignalRulesMixin:
    """Mixin providing per-setup scoring functions (gap-and-go, VWAP, mean-reversion)."""

    @staticmethod
    def score_gap_and_go(sig: dict) -> tuple[float, list[str]]:
        """
        Score a gap-and-go (opening range breakout) setup.

        Institutional logic:
          - Stock gapped up > 1.5% from prior close
          - Gap is holding (price ≥ today's open) — not filling back
          - Volume confirming the directional move
          - Stop placed just below today's open (gap fills = thesis dead, exit fast)
          - Time-bounded: only valid in the first 90 min (9:30–11:00 AM ET)

        Args:
            sig: Flat indicator dict from indicators.get_signal_summary(), with
                 gap_pct, today_open, first_bar_high, price, vol_ratio, rsi,
                 above_vwap.

        Returns:
            Tuple of (score, evidence_list). score is 0.0 and list is empty when
            outside the time window or when the gap size doesn't qualify.
        """
        now_et   = datetime.now(config.ET)
        vol_ratio = float(sig.get("vol_ratio", 1.0))
        cutoff   = now_et.hour > config.GAP_AND_GO_CUTOFF_HOUR or (
                   now_et.hour == config.GAP_AND_GO_CUTOFF_HOUR and
                   now_et.minute >= config.GAP_AND_GO_CUTOFF_MIN)
        # Catalyst exception: news-driven volume spike (vol_ratio > 3×) bypasses time gate.
        # FDA decisions, earnings revisions, and analyst upgrades can hit at any hour.
        news_catalyst = cutoff and vol_ratio >= 3.0
        if cutoff and not news_catalyst:
            return 0.0, []

        gap_pct        = float(sig.get("gap_pct",        0))
        today_open     = float(sig.get("today_open",     0))
        first_bar_high = float(sig.get("first_bar_high", 0))
        price          = float(sig.get("price",          0))
        vol_ratio      = float(sig.get("vol_ratio",      1.0))
        rsi            = float(sig.get("rsi",            50))
        above_vwap     = bool(sig.get("above_vwap",      False))
        orb_30_high    = float(sig.get("orb_30_high",    0))
        orb_30_low     = float(sig.get("orb_30_low",     0))
        orb_30_valid   = bool(sig.get("orb_30_valid",    False))
        orb_30_width   = float(sig.get("orb_30_width_pct", 0))

        if gap_pct < config.GAP_AND_GO_MIN_PCT or gap_pct > config.GAP_AND_GO_MAX_PCT:
            return 0.0, []

        # Gap filling: price dropped back below today's open → setup dead
        if today_open > 0 and price < today_open * 0.997:
            return 0.0, []

        score = 5.0
        ev    = [f"+5.0 gap-and-go {gap_pct:+.1f}% from prior close"]
        if news_catalyst:
            score -= 0.5   # small penalty for being past prime opening window
            ev.append("-0.5 after-hours catalyst (past 11 AM gate)")


        # Gap size bonus
        if gap_pct >= 4.0:
            score += 1.5; ev.append(f"+1.5 strong gap {gap_pct:.1f}%")
        elif gap_pct >= 2.5:
            score += 1.0; ev.append(f"+1.0 solid gap {gap_pct:.1f}%")
        elif gap_pct >= 1.5:
            score += 0.5; ev.append(f"+0.5 gap {gap_pct:.1f}%")

        # Price structure — tiered ORB check
        # After 10:00 ET the 30-min range is complete; that breakout is the real signal.
        # Before 10:00 fall back to first-bar high as a weaker early-session proxy.
        if orb_30_valid and orb_30_high > 0 and price > orb_30_high:
            score += 1.5; ev.append(
                f"+1.5 ORB-30 breakout — cleared 30-min institutional range ({orb_30_high:.2f})")
            # Wide ORB means the stop (at orb_30_low) is far — R:R degrades
            if orb_30_width > 3.0:
                score -= 0.5; ev.append(
                    f"-0.5 ORB-30 wide ({orb_30_width:.1f}%) — stop distance elevated")
        elif today_open > 0 and price > first_bar_high:
            score += 0.8; ev.append("+0.8 above first-bar high (ORB-30 not yet complete)")
        elif today_open > 0 and price >= today_open:
            score += 0.3; ev.append("+0.3 gap holding above today's open")

        # VWAP support
        if above_vwap:
            score += 0.5; ev.append("+0.5 above VWAP")

        # Volume
        if vol_ratio >= 3.0:
            score += 1.5; ev.append(f"+1.5 institutional volume {vol_ratio:.1f}x")
        elif vol_ratio >= 2.0:
            score += 1.0; ev.append(f"+1.0 surge volume {vol_ratio:.1f}x")
        elif vol_ratio >= 1.5:
            score += 0.5; ev.append(f"+0.5 elevated volume {vol_ratio:.1f}x")

        # Overbought penalty — gapping into already-extended RSI = chasing
        if rsi > 78:
            score -= 1.5; ev.append(f"-1.5 RSI overbought {rsi:.0f} (gap extended)")
        elif rsi > 72:
            score -= 0.8; ev.append(f"-0.8 RSI hot {rsi:.0f}")

        # Stop width check: if price is already far above open, stop is wide → poor R:R
        if today_open > 0 and price > 0:
            pct_above_open = (price - today_open) / today_open * 100
            if pct_above_open > gap_pct * 0.75:
                score -= 1.0; ev.append(
                    f"-1.0 price {pct_above_open:.1f}% above open — stop too wide, chasing")

        return round(max(0.0, min(10.0, score)), 1), ev

    @staticmethod
    def score_vwap_reclaim(sig: dict) -> tuple[float, list[str]]:
        """
        Score a VWAP reclaim (mean-reversion) setup.

        Institutional logic:
          - Sellers pushed price below VWAP; buyers reclaim it with volume.
          - Stop: just below VWAP (if it falls back, thesis is dead).
          - Target: prior high, next resistance, or ORB-30 high.
          - Valid all day — not time-limited like gap-and-go.

        Args:
            sig: Flat indicator dict from indicators.get_signal_summary(), with
                 vwap_cross_up, vol_ratio, rsi, price, vwap, ema9, ema21, ema50.

        Returns:
            Tuple of (score, evidence_list). Returns (0.0, []) when vwap_cross_up
            is False — scorer is effectively disabled until the cross actually
            occurs this bar.
        """
        if not bool(sig.get("vwap_cross_up", False)):
            return 0.0, []

        vol_ratio = float(sig.get("vol_ratio",  1.0))
        rsi       = float(sig.get("rsi",        50))
        price     = float(sig.get("price",      0))
        vwap      = float(sig.get("vwap",       0))
        ema9      = float(sig.get("ema9",       0))
        ema21     = float(sig.get("ema21",      0))
        ema50     = float(sig.get("ema50",      0))

        score = 5.0
        ev    = ["+5.0 VWAP reclaim — price crossed back above VWAP from below"]

        # Volume — the most important confirmation (institutions drive the reclaim)
        if vol_ratio >= 2.5:
            score += 1.5; ev.append(f"+1.5 strong volume on reclaim {vol_ratio:.1f}x")
        elif vol_ratio >= 1.8:
            score += 1.0; ev.append(f"+1.0 solid volume confirmation {vol_ratio:.1f}x")
        elif vol_ratio >= 1.3:
            score += 0.5; ev.append(f"+0.5 above-avg volume {vol_ratio:.1f}x")
        else:
            score -= 0.5; ev.append(f"-0.5 weak volume on reclaim {vol_ratio:.1f}x — low conviction")

        # RSI — want recovery, not already extended
        if 42 <= rsi <= 60:
            score += 0.5; ev.append(f"+0.5 RSI recovery zone {rsi:.0f}")
        elif 35 <= rsi < 42:
            score += 0.3; ev.append(f"+0.3 RSI recovering {rsi:.0f}")
        elif rsi > 70:
            score -= 1.5; ev.append(f"-1.5 RSI overbought at reclaim {rsi:.0f} — chasing")
        elif rsi > 65:
            score -= 0.8; ev.append(f"-0.8 RSI elevated {rsi:.0f}")

        # EMA trend — with-trend reclaims succeed more than countertrend
        if ema9 > ema21:
            score += 1.0; ev.append("+1.0 EMA9>EMA21 — reclaiming VWAP inside uptrend")
        elif ema9 < ema21 and ema21 < ema50 and ema50 > 0:
            score -= 1.0; ev.append("-1.0 full bearish EMA stack — countertrend reclaim")

        # Entry proximity to VWAP — tighter = better R:R (stop just below VWAP)
        if vwap > 0 and price > 0:
            pct_above = (price - vwap) / vwap * 100
            if pct_above > 2.5:
                score -= 1.0; ev.append(f"-1.0 price {pct_above:.1f}% above VWAP — stop too wide")
            elif pct_above > 1.5:
                score -= 0.5; ev.append(f"-0.5 price {pct_above:.1f}% above VWAP — entry extended")

        return round(max(0.0, min(10.0, score)), 1), ev

    @staticmethod
    def score_mean_reversion(sig: dict) -> tuple[float, list[str]]:
        """
        Score a mean-reversion-to-POC setup.

        Institutional logic:
          - Price is inside the value area but below the POC (volume cluster center).
          - POC is a statistical magnet — price tends to revisit and fill it.
          - Entry: price in the lower portion of the value area, momentum turning up.
          - Stop: below VAL (value area low — structural support).
          - Target: POC (the reversion target).

        Args:
            sig: Flat indicator dict extended with volume profile fields:
                 poc, val, in_value_area, near_poc, price, rsi, vol_ratio,
                 ema9, ema21, macd_hist, range_pct.

        Returns:
            Tuple of (score, evidence_list). Returns (0.0, []) when volume profile
            data is absent or price is not in the correct structural position.
        """
        poc           = float(sig.get("poc",          0))
        val           = float(sig.get("val",          0))
        in_value_area = bool(sig.get("in_value_area", False))
        near_poc      = bool(sig.get("near_poc",      False))
        price         = float(sig.get("price",        0))
        rsi           = float(sig.get("rsi",          50))
        vol_ratio     = float(sig.get("vol_ratio",    1.0))
        ema9          = float(sig.get("ema9",         0))
        ema21         = float(sig.get("ema21",        0))
        macd_hist     = float(sig.get("macd_hist",    0))
        range_pct     = float(sig.get("range_pct",   50))

        # Mandatory: need volume profile data, price below POC, price above VAL
        if poc == 0 or val == 0 or price <= 0:
            return 0.0, []
        if price >= poc:                    # price already at/above POC — no reversion needed
            return 0.0, []
        if price < val:                     # broken below value area — falling knife, skip
            return 0.0, []

        score = 4.5
        pct_to_poc = (poc - price) / price * 100
        ev    = [f"+4.5 mean-reversion: below POC ({poc:.2f}), VAL support {val:.2f}"]

        # Value area confirmation — inside the institutional auction zone
        if in_value_area:
            score += 1.0; ev.append("+1.0 inside value area — institutional fair value zone")

        # POC proximity — the closer the target, the better the R:R
        if near_poc:
            score += 0.8; ev.append(f"+0.8 near POC — reversion target within 0.3%")
        elif pct_to_poc <= 1.5:
            score += 0.5; ev.append(f"+0.5 POC {pct_to_poc:.1f}% away — achievable target")
        elif pct_to_poc > 4.0:
            score -= 0.5; ev.append(f"-0.5 POC {pct_to_poc:.1f}% away — R:R stretched")

        # Discount zone — price in lower half of daily range (room to rise)
        if range_pct < 35:
            score += 0.8; ev.append(f"+0.8 deep discount {range_pct:.0f}% — strong pullback entry")
        elif range_pct < 50:
            score += 0.4; ev.append(f"+0.4 discount zone {range_pct:.0f}%")

        # RSI — want recovery, not still in freefall
        if 42 <= rsi <= 58:
            score += 0.5; ev.append(f"+0.5 RSI recovery range {rsi:.0f}")
        elif 35 <= rsi < 42:
            score += 0.3; ev.append(f"+0.3 RSI recovering {rsi:.0f}")
        elif rsi < 30:
            score -= 1.5; ev.append(f"-1.5 RSI deeply oversold {rsi:.0f} — falling knife")
        elif rsi > 65:
            score -= 0.8; ev.append(f"-0.8 RSI elevated {rsi:.0f} — reversion window closing")

        # Volume participation
        if vol_ratio >= 1.2:
            score += 0.5; ev.append(f"+0.5 volume pickup {vol_ratio:.1f}x")
        elif vol_ratio < 0.7:
            score -= 0.5; ev.append(f"-0.5 thin volume {vol_ratio:.1f}x — no conviction")

        # Momentum still falling? Penalise catching the knife
        if ema9 < ema21 and macd_hist < 0:
            score -= 1.0; ev.append("-1.0 EMA bearish + MACD negative — momentum still down")
        elif ema9 > ema21:
            score += 0.5; ev.append("+0.5 EMA9>EMA21 — pullback in uptrend, with-trend reversion")

        return round(max(0.0, min(10.0, score)), 1), ev

