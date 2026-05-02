import numpy as np
import pandas as pd
from datetime import date as _date

import config
from core.database import log


class PatternsMixin:
    @staticmethod
    def compute_premium_discount(df: pd.DataFrame, lookback: int = 50) -> dict:
        """
        Locate current price within the recent high-low range (Fibonacci 50% rule).

        Institutions buy at discount (below the 50% range midpoint) and sell at
        premium (above). This mirrors the ICT premium/discount concept and the
        standard SMC entry discipline.

        Args:
            df:       OHLCV DataFrame (post compute_indicators).
            lookback: Number of bars to define the swing range.

        Returns:
            Dict with keys:
              range_high  – highest high over the lookback window
              range_low   – lowest low over the lookback window
              range_mid   – midpoint ((high + low) / 2)
              range_pct   – where current price sits (0 = at low, 100 = at high)
              in_discount – True when price ≤ midpoint (institutional buy zone)
            Empty dict if the range is too narrow (< 0.5%) or data insufficient.
        """
        try:
            if df.empty or len(df) < 10:
                return {}
            window     = df.iloc[-lookback:]
            range_high = float(window["high"].max())
            range_low  = float(window["low"].min())
            if range_high <= range_low or (range_high - range_low) / range_high < 0.005:
                return {}
            price     = float(df["close"].iloc[-1])
            range_mid = (range_high + range_low) / 2
            range_pct = (price - range_low) / (range_high - range_low) * 100
            return {
                "range_high":  round(range_high, 2),
                "range_low":   round(range_low,  2),
                "range_mid":   round(range_mid,  2),
                "range_pct":   round(range_pct,  1),
                "in_discount": price <= range_mid,
            }
        except Exception:
            return {}

    @staticmethod
    def detect_liquidity_sweep(df: pd.DataFrame, key_levels: dict | None = None,
                               lookback: int = 10) -> dict:
        """
        Detect a liquidity sweep (stop hunt) reversal — the #1 institutional entry signal.

        Banks engineer price to pierce just below retail stop clusters (prior swing lows,
        ORB lows, round numbers), collect liquidity from retail stop orders, then rapidly
        reverse. This pattern, called a "liquidity sweep" or "stop hunt," is where
        institutions build their long positions.

        Detection criteria:
          1. Price pierces below a key level (prior swing low, ORB low, or VAL from profile).
          2. Within 1–3 subsequent candles, price closes BACK ABOVE that level.
          3. The sweep bar or the reversal bar has above-average volume (confirms institutional participation).

        Args:
            df:         OHLCV DataFrame (post compute_indicators).
            key_levels: Optional dict from get_key_levels() with known support levels.
            lookback:   Number of recent bars to scan for the sweep pattern.

        Returns:
            Dict with keys when a sweep is detected:
              liquidity_sweep_detected – True
              sweep_low   – the level that was swept (now becomes strong support)
              sweep_entry – suggested entry price (first close back above sweep level)
              stop_beyond – recommended stop (0.3% below the sweep low, not at it)
            Empty dict if no sweep is detected or data is insufficient.
        """
        try:
            if df.empty or len(df) < lookback + 3:
                return {}

            price    = float(df["close"].iloc[-1])
            avg_vol  = float(df["volume"].iloc[-lookback:].mean())
            window   = df.iloc[-lookback:].reset_index(drop=True)
            n        = len(window)

            # Collect candidate levels to watch for sweeps
            levels: list[float] = []
            if key_levels:
                for k in ("nearest_support", "prev_day_low", "orb_30_low",
                          "val", "poc", "pre_market_low"):
                    v = key_levels.get(k)
                    if v and float(v) > 0:
                        levels.append(float(v))

            # Also use recent swing lows from the window itself
            for i in range(1, n - 1):
                prev_low = float(window["low"].iloc[i - 1])
                curr_low = float(window["low"].iloc[i])
                next_low = float(window["low"].iloc[i + 1])
                if curr_low < prev_low and curr_low < next_low:
                    levels.append(curr_low)

            if not levels:
                return {}

            # Look for a candle that pierced below a level then closed back above it
            for i in range(2, n):
                bar_low   = float(window["low"].iloc[i])
                bar_close = float(window["close"].iloc[i])
                bar_vol   = float(window["volume"].iloc[i])

                for level in levels:
                    if level <= 0:
                        continue
                    # Bar pierced below the level
                    if bar_low < level * 0.999 and bar_close > level:
                        high_vol = bar_vol >= avg_vol * 1.2
                        # Check if next 1–2 bars also confirm close above (pattern holds)
                        subsequent_closes_ok = all(
                            float(window["close"].iloc[j]) > level
                            for j in range(i + 1, min(i + 3, n))
                        )
                        if high_vol or subsequent_closes_ok:
                            return {
                                "liquidity_sweep_detected": True,
                                "sweep_low":    round(level, 4),
                                "sweep_entry":  round(bar_close, 4),
                                "stop_beyond":  round(bar_low * 0.997, 4),  # 0.3% below the sweep
                            }

            return {}
        except Exception:
            return {}

    @staticmethod
    def detect_fvg(df: pd.DataFrame, lookback: int = 50) -> dict:
        """
        Detect unfilled Fair Value Gaps (3-candle imbalances) near current price.

        A Fair Value Gap is formed when price moves so fast that a 3-candle
        sequence leaves an untraded zone between candle 1 and candle 3:
          Bullish FVG: candle3.low > candle1.high — up-move left a support gap.
          Bearish FVG: candle3.high < candle1.low — down-move left a resistance gap.

        An FVG is considered "unfilled" if no subsequent bar has closed inside it.
        Unfilled FVGs act as magnets — price is statistically likely to revisit
        and fill them before continuing its trend.

        Args:
            df:       OHLCV DataFrame (at least 5 rows).
            lookback: Number of recent bars to scan for FVGs.

        Returns:
            Dict with any of these keys (only present when an FVG is found):
              bullish_fvg   – {low, high, mid} of the nearest unfilled bullish gap
              near_bull_fvg – True when price is within 0.5% above the bullish FVG
              bearish_fvg   – {low, high, mid} of the nearest unfilled bearish gap
              near_bear_fvg – True when price is within 1.0% below the bearish FVG
            Empty dict if no qualifying FVGs are found.
        """
        try:
            if df.empty or len(df) < 5:
                return {}
            price    = float(df["close"].iloc[-1])
            window   = df.iloc[-lookback:].reset_index(drop=True)
            n        = len(window)
            min_size = price * 0.001  # ignore gaps < 0.1% of price (noise)

            nearest_bull: dict | None = None
            nearest_bear: dict | None = None

            for i in range(1, n - 1):
                c1_high = float(window["high"].iloc[i - 1])
                c1_low  = float(window["low"].iloc[i - 1])
                c3_high = float(window["high"].iloc[i + 1])
                c3_low  = float(window["low"].iloc[i + 1])

                # Bullish FVG: gap zone = [c1_high, c3_low]
                if c3_low > c1_high and (c3_low - c1_high) >= min_size:
                    fvg_lo, fvg_hi    = c1_high, c3_low
                    subseq_closes     = [float(window["close"].iloc[j]) for j in range(i + 2, n)]
                    unfilled          = not any(v < fvg_hi for v in subseq_closes)
                    if unfilled and fvg_hi < price:
                        if nearest_bull is None or fvg_hi > nearest_bull["high"]:
                            nearest_bull = {"low":  round(fvg_lo, 2),
                                            "high": round(fvg_hi, 2),
                                            "mid":  round((fvg_lo + fvg_hi) / 2, 2)}

                # Bearish FVG: gap zone = [c3_high, c1_low]
                if c3_high < c1_low and (c1_low - c3_high) >= min_size:
                    fvg_lo, fvg_hi    = c3_high, c1_low
                    subseq_closes     = [float(window["close"].iloc[j]) for j in range(i + 2, n)]
                    unfilled          = not any(v > fvg_lo for v in subseq_closes)
                    if unfilled and fvg_lo > price:
                        if nearest_bear is None or fvg_lo < nearest_bear["low"]:
                            nearest_bear = {"low":  round(fvg_lo, 2),
                                            "high": round(fvg_hi, 2),
                                            "mid":  round((fvg_lo + fvg_hi) / 2, 2)}

            result: dict = {}
            if nearest_bull:
                result["bullish_fvg"]   = nearest_bull
                result["near_bull_fvg"] = (price - nearest_bull["high"]) / price <= 0.005
            if nearest_bear:
                result["bearish_fvg"]   = nearest_bear
                result["near_bear_fvg"] = (nearest_bear["low"] - price) / price <= 0.010
            return result
        except Exception:
            return {}

    @staticmethod
    def compute_volume_profile(df: pd.DataFrame, n_buckets: int = 50) -> dict:
        """
        Build an intraday volume profile and extract its key structural levels.

        Distributes each bar's volume uniformly across its [low, high] price range
        into n_buckets equally-spaced bins, then identifies:

          POC (Point of Control) — bin where most volume traded today. Price
            gravitates toward POC between trends (mean-reversion magnet).
          Value Area (VA) — tightest cluster of bins containing 70% of volume.
            Inside VA: price auctions back and forth (chop zone).
            Above VAH: bullish expansion out of balance.
            Below VAL: bearish rejection from the volume cluster.
          LVN (Low-Volume Node) — bins with < 25% of average volume. These are
            "free air" zones where price moves fast with little friction.

        Uses today's session only for the most relevant intraday context.
        Falls back to the full df window if fewer than 6 session bars are available.

        Args:
            df:        5-min OHLCV DataFrame (post compute_indicators).
            n_buckets: Number of equally-spaced price bins to distribute volume into.

        Returns:
            Dict with keys:
              poc              – Point of Control price (bin midpoint)
              vah              – Value Area High (top boundary of 70% VA)
              val              – Value Area Low (bottom boundary of 70% VA)
              near_poc         – True when price is within 0.3% of POC
              in_value_area    – True when val ≤ price ≤ vah
              above_value_area – True when price > vah (bullish expansion zone)
              below_value_area – True when price < val (rejected from volume support)
              lvn_above        – nearest LVN price above current price, or None
            Empty dict if data is insufficient or the price range is too narrow.
        """
        try:
            if df.empty or len(df) < 10:
                return {}

            price = float(df["close"].iloc[-1])

            # Isolate today's session using a UTC-midnight anchor (no Python date loop)
            try:
                _last = df.index[-1]
                _et_l = _last.tz_convert(config.ET) if df.index.tz is not None else _last
                _open = _et_l.replace(hour=9, minute=30, second=0, microsecond=0)
                _open_utc = _open.tz_convert("UTC") if _open.tzinfo is not None else _open
                df_session = df[df.index >= _open_utc]
                if len(df_session) < 6:
                    df_session = df
            except Exception:
                df_session = df

            p_min = float(df_session["low"].min())
            p_max = float(df_session["high"].max())
            if p_max <= p_min or (p_max - p_min) / p_max < 0.001:
                return {}

            bucket_size = (p_max - p_min) / n_buckets
            vol_profile = np.zeros(n_buckets)

            # Vectorized distribution: avoid pandas row-by-row overhead
            lows  = np.asarray(df_session["low"],    dtype=float)
            highs = np.asarray(df_session["high"],   dtype=float)
            vols  = np.asarray(df_session["volume"], dtype=float)
            valid = (vols > 0) & (highs > lows)
            lows, highs, vols = lows[valid], highs[valid], vols[valid]

            lo_idxs = np.clip(((lows  - p_min) / bucket_size).astype(int), 0, n_buckets - 1)
            hi_idxs = np.clip(((highs - p_min) / bucket_size).astype(int), 0, n_buckets - 1)
            for lo, hi, v in zip(lo_idxs, hi_idxs, vols):
                vol_profile[lo : hi + 1] += v / (hi - lo + 1)

            total_vol = vol_profile.sum()
            if total_vol <= 0:
                return {}

            # ── POC ──────────────────────────────────────────────────────────────
            poc_idx   = int(np.argmax(vol_profile))
            poc_price = round(p_min + (poc_idx + 0.5) * bucket_size, 2)

            # ── Value Area (expand from POC until 70% of volume is enclosed) ─────
            target   = total_vol * 0.70
            lo_b, hi_b = poc_idx, poc_idx
            va_vol   = vol_profile[poc_idx]
            while va_vol < target:
                can_lo = lo_b > 0
                can_hi = hi_b < n_buckets - 1
                if not can_lo and not can_hi:
                    break
                add_lo = vol_profile[lo_b - 1] if can_lo else 0.0
                add_hi = vol_profile[hi_b + 1] if can_hi else 0.0
                if add_hi >= add_lo:
                    hi_b += 1; va_vol += add_hi
                else:
                    lo_b -= 1; va_vol += add_lo

            vah = round(p_min + (hi_b + 1) * bucket_size, 2)
            val = round(p_min + lo_b       * bucket_size, 2)

            # ── LVNs (bins below 25% of average — free air zones) ────────────────
            filled      = vol_profile[vol_profile > 0]
            avg_vol     = float(filled.mean()) if filled.size > 0 else 0.0
            lvn_thresh  = avg_vol * 0.25
            lvn_prices  = [
                round(p_min + (idx + 0.5) * bucket_size, 2)
                for idx in range(n_buckets)
                if 0 < vol_profile[idx] < lvn_thresh
            ]
            lvn_above = next((lv for lv in sorted(lvn_prices) if lv > price * 1.001), None)

            return {
                "poc":              poc_price,
                "vah":              vah,
                "val":              val,
                "near_poc":         abs(price - poc_price) / price <= 0.003,
                "in_value_area":    val <= price <= vah,
                "above_value_area": price > vah,
                "below_value_area": price < val,
                "lvn_above":        lvn_above,
            }

        except Exception:
            return {}

    @staticmethod
    def get_key_levels(df_5m: pd.DataFrame,
                       df_day: pd.DataFrame | None) -> dict:
        """
        Identify key horizontal price levels that institutions watch and trade from.

        Aggregates levels from three independent sources without any extra API calls:
          1. Daily bars  — prev day H/L/C, 5-day (weekly) high/low.
          2. Pre-market  — today's high/low between 4:00–9:29 AM ET (most-watched
                           single reference by professional intraday traders).
          3. 60-min swings — resampled from the already-fetched 5-min bars;
                             pivot highs/lows confirmed by 2 bars on each side.

        All levels are merged, de-duplicated within 0.1% of each other, and split
        into resistance (above price) and support (below price) lists.

        Args:
            df_5m:  5-min OHLCV DataFrame (DatetimeIndex, at least 20 rows).
            df_day: Daily OHLCV DataFrame, or None if unavailable.

        Returns:
            Dict with any of these keys (only present when data supports them):
              prev_day_high / prev_day_low / prev_day_close
              week_high / week_low
              premarket_high / premarket_low
              resistance_levels  – sorted ascending list above price (up to 5)
              support_levels     – sorted descending list below price (up to 5)
              nearest_resistance – closest resistance above price (primary TP target)
              nearest_support    – closest support below price (entry confirmation zone)
            Empty dict if df_5m is None, empty, or has fewer than 20 rows.
        """
        result: dict = {}
        if df_5m is None or df_5m.empty or len(df_5m) < 20:
            return result

        try:
            price = float(df_5m["close"].iloc[-1])

            # ── Daily levels ──────────────────────────────────────────────────────
            if df_day is not None and len(df_day) >= 2:
                yd = df_day.iloc[-2]
                result["prev_day_high"]  = round(float(yd["high"]),  2)
                result["prev_day_low"]   = round(float(yd["low"]),   2)
                result["prev_day_close"] = round(float(yd["close"]), 2)
            if df_day is not None and len(df_day) >= 5:
                w = df_day.iloc[-5:]
                result["week_high"] = round(float(w["high"].max()), 2)
                result["week_low"]  = round(float(w["low"].min()),  2)

            # ── Pre-market high/low (4:00–9:29 AM ET today) ───────────────────────
            try:
                dti      = pd.DatetimeIndex(df_5m.index)
                idx_et   = dti.tz_convert(config.ET) if dti.tz else dti.tz_localize("UTC").tz_convert(config.ET)
                today_et = _date.today()
                pm_mask  = [
                    (ts.date() == today_et)
                    and (4 <= ts.hour)
                    and (ts.hour < 9 or (ts.hour == 9 and ts.minute < 30))
                    for ts in idx_et
                ]
                df_pre = df_5m[pm_mask]
                if not df_pre.empty:
                    result["premarket_high"] = round(float(df_pre["high"].max()), 2)
                    result["premarket_low"]  = round(float(df_pre["low"].min()),  2)
            except Exception:
                pass

            # ── 60-min swing highs/lows (resampled — no extra API call) ──────────
            df_60 = df_5m.resample("60min").agg({
                "open":   "first",
                "high":   "max",
                "low":    "min",
                "close":  "last",
                "volume": "sum",
            }).dropna()

            swing_highs: list[float] = []
            swing_lows:  list[float] = []
            if len(df_60) >= 5:
                highs = list(df_60["high"])
                lows  = list(df_60["low"])
                for i in range(2, len(highs) - 2):
                    if (highs[i] > highs[i-1] and highs[i] > highs[i+1] and
                            highs[i] > highs[i-2] and highs[i] > highs[i+2]):
                        swing_highs.append(round(float(highs[i]), 2))
                    if (lows[i] < lows[i-1] and lows[i] < lows[i+1] and
                            lows[i] < lows[i-2] and lows[i] < lows[i+2]):
                        swing_lows.append(round(float(lows[i]), 2))

            # ── Merge and classify levels; filter within 0.1% of current price ───
            TOL = 0.001
            all_res = [v for v in [
                result.get("prev_day_high"),
                result.get("week_high"),
                result.get("premarket_high"),
            ] + swing_highs if v]
            all_sup = [v for v in [
                result.get("prev_day_low"),
                result.get("prev_day_close"),
                result.get("week_low"),
                result.get("premarket_low"),
            ] + swing_lows if v]

            resistance_levels = sorted(
                {lv for lv in all_res if lv > price * (1 + TOL)}
            )[:5]
            support_levels = sorted(
                {lv for lv in all_sup if 0 < lv < price * (1 - TOL)},
                reverse=True,
            )[:5]

            result["resistance_levels"]  = resistance_levels
            result["support_levels"]     = support_levels
            result["nearest_resistance"] = resistance_levels[0] if resistance_levels else None
            result["nearest_support"]    = support_levels[0]    if support_levels    else None

        except Exception:
            pass

        return result

