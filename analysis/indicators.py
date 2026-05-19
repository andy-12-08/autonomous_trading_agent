import numpy as np
import pandas as pd

import config
from core.database import log


from analysis.patterns import PatternsMixin

class IndicatorEngine(PatternsMixin):
    """Compute technical indicators and summarize them for scanner and scorer layers."""

    @staticmethod
    def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
        """
        Compute all technical indicators and append them as columns on df.

        Requires at least 20 rows; returns df unchanged if the guard is not met.
        Mutates df in-place and also returns it for chaining convenience.

        Args:
            df: OHLCV DataFrame with columns open/high/low/close/volume
                and a DatetimeIndex (UTC-aware or naive).

        Returns:
            The same DataFrame with added columns:
              ema9, ema21, ema50, macd, macd_signal, macd_hist,
              rsi, bb_mid, bb_up, bb_lo, bb_pct, atr,
              vol_sma20, vol_ratio, vwap (intraday reset), mom10,
              gap_pct, today_open, first_bar_high, first_bar_low,
              orb_30_high, orb_30_low, orb_30_valid, orb_30_width_pct.
        """
        if df.empty or len(df) < 20:
            return df

        close = df["close"]
        high  = df["high"]
        low   = df["low"]
        vol   = df["volume"]

        # -- Trend EMAs ------------------------------------------------------------
        df["ema9"]  = close.ewm(span=9,  adjust=False).mean()
        df["ema21"] = close.ewm(span=21, adjust=False).mean()
        df["ema50"] = close.ewm(span=50, adjust=False).mean()

        # -- MACD (12/26/9) --------------------------------------------------------
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        df["macd"]        = ema12 - ema26
        df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
        df["macd_hist"]   = df["macd"] - df["macd_signal"]

        # -- RSI (14, computed via EMA smoothing) ----------------------------------
        delta = close.diff()
        avg_g = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
        avg_l = (-delta).clip(lower=0).ewm(com=13, adjust=False).mean()
        df["rsi"] = 100 - 100 / (1 + avg_g / avg_l.replace(0, np.nan))

        # -- Bollinger Bands (20, 2s) ----------------------------------------------
        sma20        = close.rolling(20).mean()
        std20        = close.rolling(20).std()
        df["bb_mid"] = sma20
        df["bb_up"]  = sma20 + 2 * std20
        df["bb_lo"]  = sma20 - 2 * std20
        df["bb_pct"] = (close - df["bb_lo"]) / (df["bb_up"] - df["bb_lo"])

        # -- ATR (14, EMA-smoothed true range) ------------------------------------
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        df["atr"] = tr.ewm(com=13, adjust=False).mean()

        # -- Volume ratio: today's cumulative volume vs expected pace -------------
        df["vol_sma20"] = vol.rolling(20).mean()

        # Scalar time-adjusted vol_ratio: today's cumulative volume vs expected pace.
        # Only the last-bar value is used downstream  no need for a full time series.
        _last_ts   = df.index[-1]
        _et_last   = _last_ts.tz_convert(config.ET) if df.index.tz is not None else _last_ts
        # Today's session open as a UTC-comparable timestamp
        _et_open   = _et_last.replace(hour=9, minute=30, second=0, microsecond=0)
        _utc_open  = _et_open.tz_convert("UTC") if _et_open.tzinfo is not None else _et_open
        _today_mask  = df.index >= _utc_open
        _today_bars  = int(_today_mask.sum())
        _today_vol   = float(vol[_today_mask].sum())
        _elapsed     = max(1, min(_et_last.hour * 60 + _et_last.minute - 570, 390))
        _avg_dvol    = float(df["vol_sma20"].iloc[-1]) * 78
        _expected    = _avg_dvol * (_elapsed / 390.0)
        if _today_vol == 0 and _today_bars > 0:
            # Today's bars exist but all report zero volume  IEX data gap.
            # Fall back to neutral rather than falsely vetoing the symbol.
            log.warning("vol_ratio IEX gap: %d today-bars all zero volume "
                        "(avg_dvol=%.0f expected=%.0f elapsed=%dm)  using 1.0",
                        _today_bars, _avg_dvol, _expected, _elapsed)
            _vol_ratio = 1.0
        elif _expected > 0:
            _vol_ratio = min(_today_vol / _expected, 10.0)
        elif _today_bars > 0:
            # No historical volume average (all bars zero) but today has bars  data gap.
            log.warning("vol_ratio IEX gap: avg_dvol=0 with %d today-bars  using 1.0", _today_bars)
            _vol_ratio = 1.0
        else:
            _vol_ratio = 0.0
        df["vol_ratio"] = _vol_ratio

        # -- VWAP (intraday reset  cumulates per calendar day, not across days) ---
        _day_key = [t.date() for t in df.index]   # list of Python date objects  groupby key
        tp = (high + low + close) / 3
        df["vwap"] = (
            (tp * vol).groupby(_day_key).cumsum()
            / vol.groupby(_day_key).cumsum()
        )

        # -- VWAP reclaim detection ------------------------------------------------
        # True on bars where price crosses BACK above VWAP after being below it.
        # This is the institutional mean-reversion entry signal (VWAP reclaim long).
        df["vwap_cross_up"] = (
            (df["close"] > df["vwap"]) &
            (df["close"].shift(1) <= df["vwap"].shift(1))
        ).fillna(False)

        # -- 10-bar price momentum -------------------------------------------------
        df["mom10"] = close.pct_change(10, fill_method=None) * 100

        # -- Gap detection (today's open vs prior session's last close) ------------
        try:
            # Use UTC-midnight anchor (no per-row date objects) to split today vs prior
            _today_mask  = df.index >= _utc_open
            _prior_mask  = df.index <  _utc_open

            if _today_mask.any() and _prior_mask.any():
                today_open_val = float(df["open"][_today_mask].iloc[0])
                prev_close_val = float(df["close"][_prior_mask].iloc[-1])
                first_bar_high = float(df["high"][_today_mask].iloc[0])
                first_bar_low  = float(df["low"][_today_mask].iloc[0])
                gap_pct_val    = (today_open_val - prev_close_val) / prev_close_val * 100
            else:
                today_open_val = float(df["open"].iloc[-1])
                prev_close_val = float(df["close"].iloc[-1])
                first_bar_high = float(df["high"].iloc[-1])
                first_bar_low  = float(df["low"].iloc[-1])
                gap_pct_val    = 0.0

            df["gap_pct"]        = gap_pct_val
            df["today_open"]     = today_open_val
            df["first_bar_high"] = first_bar_high
            df["first_bar_low"]  = first_bar_low
        except Exception:
            df["gap_pct"]        = 0.0
            df["today_open"]     = float(df["open"].iloc[-1])
            df["first_bar_high"] = float(df["high"].iloc[-1])
            df["first_bar_low"]  = float(df["low"].iloc[-1])

        # -- 30-minute Opening Range Breakout (ORB-30) -----------------------------
        # Institutions use the first 30 minutes (6 bars at 5-min) to establish the
        # genuine opening range after the noisy first-bar chaos settles.
        # A breakout above orb_30_high with volume is the standard gap-and-go signal.
        # orb_30_valid is only True once all 6 bars are recorded (after 10:00 ET).
        try:
            dates_list = [t.date() for t in df.index]
            today_d    = dates_list[-1]
            t_pos      = [i for i, d in enumerate(dates_list) if d == today_d]
            p_pos      = [i for i, d in enumerate(dates_list) if d <  today_d]

            if t_pos and p_pos:
                orb_bars        = t_pos[:6]
                orb_30_high_val = float(df["high"].iloc[orb_bars].max())
                orb_30_low_val  = float(df["low"].iloc[orb_bars].min())

                orb_30_valid_val = False
                if len(t_pos) >= 6:
                    try:
                        dti    = pd.DatetimeIndex(df.index)
                        idx_et = (dti.tz_convert(config.ET) if dti.tz
                                  else dti.tz_localize("UTC").tz_convert(config.ET))
                        bar6_t = idx_et[t_pos[5]]
                        # Valid once the 6th bar (9:5510:00) is present
                        orb_30_valid_val = (
                            bar6_t.hour > 9 or
                            (bar6_t.hour == 9 and bar6_t.minute >= 55)
                        )
                    except Exception:
                        orb_30_valid_val = True
            else:
                orb_30_high_val  = float(df["high"].iloc[-1])
                orb_30_low_val   = float(df["low"].iloc[-1])
                orb_30_valid_val = False

            orb_30_width_pct = (
                (orb_30_high_val - orb_30_low_val) / orb_30_low_val * 100
                if orb_30_low_val > 0 else 0.0
            )
            df["orb_30_high"]      = orb_30_high_val
            df["orb_30_low"]       = orb_30_low_val
            df["orb_30_valid"]     = orb_30_valid_val
            df["orb_30_width_pct"] = round(orb_30_width_pct, 3)
        except Exception:
            df["orb_30_high"]      = df["first_bar_high"]
            df["orb_30_low"]       = df["first_bar_low"]
            df["orb_30_valid"]     = False
            df["orb_30_width_pct"] = 0.0

        return df

    @staticmethod
    def compute_relative_strength(df_stock: pd.DataFrame,
                                  df_spy: pd.DataFrame,
                                  lookback: int = 5) -> float | None:
        """
        Compute relative strength of a stock vs SPY over the last N bars.

        RS = stock's N-bar return / SPY's N-bar return.
        Values above 1.0 indicate the stock is outperforming the broad market
        (institutional accumulation signal). Negative values mean the stock is
        falling while SPY rises  a distribution warning, avoid longs.

        Args:
            df_stock: 5-min OHLCV DataFrame for the candidate stock.
            df_spy:   5-min OHLCV DataFrame for SPY (same timeframe).
            lookback: Number of bars to measure the return window over.

        Returns:
            RS ratio rounded to 2 decimal places, or None when SPY is
            essentially flat (abs return < 0.05%)  dividing by near-zero
            produces meaningless extreme values.
        """
        try:
            if len(df_stock) < lookback + 1 or len(df_spy) < lookback + 1:
                return None
            stock_ret = (float(df_stock["close"].iloc[-1]) /
                         float(df_stock["close"].iloc[-(lookback + 1)]) - 1) * 100
            spy_ret   = (float(df_spy["close"].iloc[-1]) /
                         float(df_spy["close"].iloc[-(lookback + 1)]) - 1) * 100
            if abs(spy_ret) < 0.05:
                return None
            return round(stock_ret / spy_ret, 2)
        except Exception:
            return None

    @staticmethod
    def get_higher_tf_bias(df: pd.DataFrame | None) -> dict:
        """
        Derive the trend bias from a higher-timeframe DataFrame (15-min or daily).

        Reuses compute_indicators() and get_signal_summary() as the single source
        of truth for indicator math  no parallel calculation paths.

        Args:
            df: OHLCV DataFrame at the higher timeframe (15-min or daily).
                None or fewer than 10 rows returns an empty dict (fail-open).

        Returns:
            Compact dict used by signal_scorer for multi-timeframe confirmation:
              ema_bull    True when EMA9 > EMA21 (short-term uptrend)
              above_vwap  True when close > VWAP
              macd_bull   True when MACD histogram is positive
              rsi         RSI value (float)
              ema50_bull  True when close > EMA50, or None if EMA50 is zero
            Empty dict on any failure or insufficient data.
        """
        if df is None or df.empty or len(df) < 10:
            return {}
        try:
            df2 = IndicatorEngine.compute_indicators(df)
            if df2.empty:
                return {}
            sig = IndicatorEngine.get_signal_summary(df2)
            return {
                "ema_bull":   bool(sig["ema9"] > sig["ema21"]),
                "above_vwap": bool(sig["above_vwap"]),
                "macd_bull":  bool(sig["macd_hist"] > 0),
                "rsi":        sig["rsi"],
                "ema50_bull": bool(sig["price"] > sig["ema50"]) if sig.get("ema50", 0) > 0 else None,
            }
        except Exception:
            return {}

    @staticmethod
    def get_signal_summary(df: pd.DataFrame) -> dict:
        """
        Extract the latest indicator snapshot as a flat dict for the AI prompt.

        Reads the final row (and second-to-last for MACD crossover detection).
        All values are rounded to reduce JSON payload size.

        Args:
            df: OHLCV DataFrame that has been processed by compute_indicators().
                Must have at least 1 row.

        Returns:
            Flat dict of the most recent bar's indicator values:
              price, ema9, ema21, ema50, macd, macd_hist,
              macd_cross ("bullish" | "bearish" | "neutral"),
              rsi, bb_pct, atr, vwap, vol_ratio, mom10,
              above_vwap (bool), ema_trend ("bullish" | "bearish"),
              gap_pct, today_open, first_bar_high, first_bar_low,
              gap_holding (bool), above_first_bar_high (bool),
              orb_30_high, orb_30_low, orb_30_valid (bool),
              orb_30_width_pct, above_orb_30 (bool).
            Empty dict if df is empty.
        """
        if df.empty:
            return {}
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else last

        orb_30_high  = float(last.get("orb_30_high",  0))
        orb_30_low   = float(last.get("orb_30_low",   0))
        orb_30_valid = bool(last.get("orb_30_valid",  False))
        price        = float(last["close"])

        return {
            "price":       round(price, 4),
            "ema9":        round(float(last["ema9"]),  4),
            "ema21":       round(float(last["ema21"]), 4),
            "ema50":       round(float(last.get("ema50", 0)), 4),
            "macd":        round(float(last["macd"]),  4),
            "macd_hist":   round(float(last["macd_hist"]), 4),
            "macd_cross":  "bullish" if (last["macd_hist"] > 0 and prev["macd_hist"] <= 0)
                           else "bearish" if (last["macd_hist"] < 0 and prev["macd_hist"] >= 0)
                           else "neutral",
            "rsi":         round(float(last["rsi"]), 2),
            "bb_pct":      round(float(last["bb_pct"]), 3),
            "atr":         round(float(last["atr"]), 4),
            "vwap":        round(float(last["vwap"]), 4),
            "vol_ratio":   round(float(last["vol_ratio"]), 2),
            "mom10":       round(float(last["mom10"]), 3),
            "above_vwap":  bool(price > float(last["vwap"])),
            "ema_trend":   "bullish" if last["ema9"] > last["ema21"] else "bearish",
            # -- Gap / opening range -----------------------------------------------
            "gap_pct":        round(float(last.get("gap_pct",        0)), 2),
            "today_open":     round(float(last.get("today_open",     0)), 4),
            "first_bar_high": round(float(last.get("first_bar_high", 0)), 4),
            "first_bar_low":  round(float(last.get("first_bar_low",  0)), 4),
            "gap_holding":    bool(price >= float(last.get("today_open", 0)))
                              if last.get("today_open", 0) > 0 else False,
            "above_first_bar_high": bool(price >= float(last.get("first_bar_high", 0)))
                                     if last.get("first_bar_high", 0) > 0 else False,
            # -- 30-minute Opening Range Breakout ----------------------------------
            # orb_30_valid = True only after all 6 opening bars (10:00 ET) are recorded.
            # above_orb_30 = price cleared the institutional range  primary ORB signal.
            "orb_30_high":      round(orb_30_high, 4),
            "orb_30_low":       round(orb_30_low,  4),
            "orb_30_valid":     orb_30_valid,
            "orb_30_width_pct": round(float(last.get("orb_30_width_pct", 0)), 3),
            "above_orb_30":     bool(price > orb_30_high) if (orb_30_valid and orb_30_high > 0) else False,
            # -- VWAP reclaim ------------------------------------------------------
            # True when price crossed back above VWAP this bar after being below it.
            "vwap_cross_up":   bool(last.get("vwap_cross_up", False)),
            # Volume on the bar BEFORE the current one  used to detect single-candle
            # volume spikes vs sustained buying pressure on VWAP reclaim setups.
            "prev_vol_ratio":  round(float(prev.get("vol_ratio", 1.0)), 2),
        }
