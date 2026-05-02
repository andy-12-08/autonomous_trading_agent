import yfinance as yf
import pandas as pd
from datetime import date, datetime

import config
from core.database import log


class PreMarketAnalyzer:
    PM_GAP_SIGNIFICANT = 0.5

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}
        self._cache_date: str = ""
        self._ET = config.ET

    def get_premarket_data(self, symbols: list[str], force_refresh: bool = False) -> dict[str, dict]:
        """Return pre-market analysis for each symbol.

        Args:
            symbols: List of ticker symbols to analyze.
            force_refresh: If True, bypass the date-keyed cache and re-fetch.

        Returns:
            A dict mapping symbol to a dict containing:
                gap_pct         -- % change vs yesterday's close (signed)
                gap_direction   -- "up" | "down" | "flat"
                pm_high         -- pre-market session high (intraday resistance if gap-up)
                pm_low          -- pre-market session low  (intraday support if gap-down)
                pm_volume       -- total pre-market share volume
                prev_close      -- yesterday's closing price
                last_pm_price   -- most recent pre-market traded price
            Symbols with no extended-hours data are absent from the result.
        """
        today = date.today().isoformat()
        if not force_refresh and self._cache and self._cache_date == today:
            return {s: self._cache[s] for s in symbols if s in self._cache}

        result: dict[str, dict] = {}
        batch_size = 40   # yfinance handles ~40 tickers per download comfortably

        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            self._fetch_batch(batch, result)

        self._cache.update(result)
        self._cache_date = today
        log.info("Pre-market: fetched %d/%d symbols with extended-hours data",
                 len(result), len(symbols))
        return {s: result[s] for s in symbols if s in result}

    def _fetch_batch(self, batch: list[str], result: dict) -> None:
        """Download and parse pre-market bars for a batch of symbols.

        Args:
            batch: List of ticker symbols in this batch (max ~40).
            result: Mutable dict to populate with parsed pre-market data.
        """
        if not batch:
            return
        try:
            data = yf.download(
                tickers=batch,
                period="2d",
                interval="1m",
                prepost=True,
                group_by="ticker",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
        except Exception as e:
            log.warning("Pre-market: download failed for batch %s: %s", batch[:3], e)
            return

        if data is None or data.empty:
            return

        today_dt = datetime.now(self._ET).date()
        multi = isinstance(data.columns, pd.MultiIndex)

        for sym in batch:
            try:
                if multi:
                    lvl0 = data.columns.get_level_values(0)  # type: ignore[union-attr]
                    if sym not in lvl0:
                        continue
                    df = data[sym].dropna(subset=["Close"])  # type: ignore[index]
                else:
                    df = data.dropna(subset=["Close"])

                if df.empty:
                    continue

                # Ensure timezone-aware index in ET
                idx = pd.DatetimeIndex(df.index)
                if idx.tzinfo is None:
                    idx = idx.tz_localize("UTC")
                df.index = idx.tz_convert(self._ET)
                idx = pd.DatetimeIndex(df.index)

                # Yesterday's last regular-hours close (before today's pre-market)
                yesterday_bars = df[idx.date < today_dt]
                if yesterday_bars.empty:
                    continue
                prev_close = float(yesterday_bars["Close"].iloc[-1])
                if prev_close <= 0:
                    continue

                # Today's pre-market bars: 4:00 AM – 9:29 AM ET
                pm_mask = (
                    (idx.date == today_dt) &
                    (idx.hour >= 4) &
                    ((idx.hour < 9) | ((idx.hour == 9) & (idx.minute < 30)))
                )
                pm_bars = df[pm_mask]
                if pm_bars.empty:
                    continue

                pm_high   = float(pm_bars["High"].max())
                pm_low    = float(pm_bars["Low"].min())
                pm_volume = int(pm_bars["Volume"].sum())
                last_px   = float(pm_bars["Close"].iloc[-1])
                gap_pct   = (last_px - prev_close) / prev_close * 100

                result[sym] = {
                    "gap_pct":       round(gap_pct, 2),
                    "gap_direction": "up" if gap_pct >= self.PM_GAP_SIGNIFICANT
                                     else "down" if gap_pct <= -self.PM_GAP_SIGNIFICANT
                                     else "flat",
                    "pm_high":       round(pm_high, 4),
                    "pm_low":        round(pm_low, 4),
                    "pm_volume":     pm_volume,
                    "prev_close":    round(prev_close, 4),
                    "last_pm_price": round(last_px, 4),
                }
            except Exception as e:
                log.debug("Pre-market: error for %s: %s", sym, e)

    def get_premarket_key_levels(self, symbol: str) -> dict | None:
        """Return pm_high and pm_low as supplementary key levels for a single symbol.

        Intended to be merged into the existing key_levels dict in
        trader._key_levels_cache so the risk manager uses realistic structural
        levels rather than just ATR-derived guesses.

        Args:
            symbol: The ticker symbol to look up.

        Returns:
            A dict with pre_market_high, pre_market_low, and prev_close keys,
            or None if no pre-market data is available for the symbol.
        """
        data = self.get_premarket_data([symbol])
        d = data.get(symbol)
        if not d:
            return None
        return {
            "pre_market_high": d["pm_high"],
            "pre_market_low":  d["pm_low"],
            "prev_close":      d["prev_close"],
        }

    def premarket_summary(self, symbols: list[str]) -> str:
        """Return a one-line log summary of gap leaders.

        Args:
            symbols: List of ticker symbols to summarize.

        Returns:
            A human-readable string listing the top gap-up and gap-down names.
        """
        data = self.get_premarket_data(symbols)
        if not data:
            return "pre-market: no data"
        up   = sorted([(s, d) for s, d in data.items() if d["gap_direction"] == "up"],
                      key=lambda x: x[1]["gap_pct"], reverse=True)[:5]
        down = sorted([(s, d) for s, d in data.items() if d["gap_direction"] == "down"],
                      key=lambda x: x[1]["gap_pct"])[:5]
        parts = []
        if up:
            parts.append("gap up: " + ", ".join(f"{s}({d['gap_pct']:+.1f}%)" for s, d in up))
        if down:
            parts.append("gap down: " + ", ".join(f"{s}({d['gap_pct']:+.1f}%)" for s, d in down))
        return "pre-market — " + " | ".join(parts) if parts else "pre-market: all flat"
