import argparse
import smtplib
from datetime import datetime
from email.mime.text import MIMEText

import numpy as np
import pandas as pd

import config
from core.database import log


class Backtester:
    SCORE_BANDS = [
        (5.0,  6.5,  "5.0–6.5  (below gate)"),
        (6.5,  7.5,  "6.5–7.5  (marginal)"),
        (7.5,  8.5,  "7.5–8.5  (strong)"),
        (8.5, 10.1,  "8.5–10.0 (high-conviction)"),
    ]

    MAX_HOLD_BARS  = 78   # max look-forward per trade (~6.5 hours / rest of day)
    COOLDOWN_BARS  = 24   # ~2 hours cooldown after each entry (avoid re-entering same move)
    MIN_BARS_REQD  = 100  # skip symbols with fewer historical bars

    def __init__(self, broker, indicators, signal_scorer):
        """Args:
            broker: Alpaca client with get_bars_multi.
            indicators: IndicatorEngine instance.
            signal_scorer: SignalScorer instance.
        """
        self.broker        = broker
        self.indicators    = indicators
        self.signal_scorer = signal_scorer

    @staticmethod
    def _resample(df5: pd.DataFrame, rule: str) -> pd.DataFrame:
        """Resample 5-min OHLCV to a coarser timeframe.

        Args:
            df5: DataFrame with 5-minute OHLCV bars.
            rule: Pandas resample rule string (e.g. "15min", "1D").

        Returns:
            Resampled DataFrame with rows that had all-NaN values dropped.
        """
        df: pd.DataFrame = df5.resample(rule).agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        })
        return df.dropna(how="all")

    def _bias_at(self, df_htf: pd.DataFrame, ts: object) -> dict:
        """Return higher-TF trend bias for the bar immediately before timestamp ts.

        Uses pre-computed indicators — no recomputation per call.

        Args:
            df_htf: Higher-timeframe DataFrame with pre-computed indicators.
            ts: Timestamp to look up (uses ffill to find the nearest preceding bar).

        Returns:
            Dict with keys ema_bull, above_vwap, macd_bull, rsi, ema50_bull,
            or an empty dict if insufficient data or key errors occur.
        """
        if df_htf.empty:
            return {}
        idx = df_htf.index.get_indexer([ts], method="ffill")[0]
        if idx < 10:
            return {}
        last = df_htf.iloc[idx]
        try:
            return {
                "ema_bull":   bool(last["ema9"]      > last["ema21"]),
                "above_vwap": bool(last["close"]     > last["vwap"]),
                "macd_bull":  bool(last["macd_hist"] > 0),
                "rsi":        float(last["rsi"]),
                "ema50_bull": bool(last["close"]     > last.get("ema50", last["close"])),
            }
        except (KeyError, ValueError):
            return {}

    @staticmethod
    def _look_forward(df5: pd.DataFrame, start: int,
                      stop: float, tp: float) -> tuple[str, int, float]:
        """Scan forward from bar 'start' to find which is hit first: stop or target.

        Conservative: if both are breached in the same bar, stop wins.

        Args:
            df5: 5-minute OHLCV DataFrame.
            start: Index of the bar immediately after entry.
            stop: Stop-loss price level.
            tp: Take-profit price level.

        Returns:
            Tuple of (outcome, bars_held, exit_price) where outcome is one of
            "win", "loss", or "timeout".
        """
        end = min(start + Backtester.MAX_HOLD_BARS, len(df5))
        for j in range(start, end):
            bar = df5.iloc[j]
            if float(bar["low"])  <= stop: return "loss",    j - start + 1, stop
            if float(bar["high"]) >= tp:   return "win",     j - start + 1, tp
        # Neither hit — exit at last bar's close (could be win or loss)
        last_close = float(df5.iloc[end - 1]["close"])
        return "timeout", end - start, last_close

    def simulate_symbol(self, symbol: str, df5: pd.DataFrame) -> list[dict]:
        """Roll through every 5-min bar for one symbol and simulate trades.

        Scores each bar; simulates entry where score >= MIN_SIGNAL_SCORE_TO_AI.

        Args:
            symbol: Ticker symbol being simulated.
            df5: 5-minute OHLCV DataFrame for the symbol.

        Returns:
            List of trade dicts, each containing symbol, ts, score, outcome,
            hold_bars, pnl_pct.
        """
        if df5.empty or len(df5) < Backtester.MIN_BARS_REQD:
            return []

        # Compute indicators once — reused for every bar via .iloc[-1] access
        df5    = self.indicators.compute_indicators(df5.copy())
        df15   = self.indicators.compute_indicators(Backtester._resample(df5, "15min"))
        df_day = self.indicators.compute_indicators(Backtester._resample(df5, "1D"))

        trades: list[dict] = []
        cooldown = 0

        # Start from bar 60 (need EMA50 + buffer); leave MAX_HOLD_BARS at the end
        for i in range(60, len(df5) - Backtester.MAX_HOLD_BARS - 1):
            if cooldown > 0:
                cooldown -= 1
                continue

            # 2-row slice is sufficient: get_signal_summary reads .iloc[-1] and .iloc[-2]
            sig = self.indicators.get_signal_summary(df5.iloc[i - 1 : i + 1])
            if not sig:
                continue

            ts       = df5.index[i]
            bias_15  = self._bias_at(df15,   ts)
            bias_day = self._bias_at(df_day, ts)

            score, _ = self.signal_scorer.score_setup(sig, bias_15=bias_15, bias_day=bias_day)

            if score < config.MIN_SIGNAL_SCORE_TO_AI:
                continue

            entry = float(df5.iloc[i]["close"])
            atr   = float(sig.get("atr") or entry * 0.015)
            stop  = entry - max(
                atr   * config.ATR_STOP_MULTIPLIER,
                entry * config.DEFAULT_STOP_LOSS_PCT,
            )
            risk = entry - stop
            if risk <= 0:
                continue
            tp = entry + risk * config.MIN_REWARD_TO_RISK

            outcome, hold_bars, exit_price = Backtester._look_forward(df5, i + 1, stop, tp)
            pnl_pct = (exit_price - entry) / entry * 100

            trades.append({
                "symbol":    symbol,
                "ts":        str(ts),
                "score":     round(score, 2),
                "outcome":   outcome,
                "hold_bars": hold_bars,
                "pnl_pct":   round(pnl_pct, 3),
            })

            cooldown = hold_bars + Backtester.COOLDOWN_BARS

        log.info("Backtest %s: %d setups in %d bars", symbol, len(trades), len(df5))
        return trades

    def analyze_results(self, trades: list[dict]) -> dict:
        """Group trades by score band and compute win rate and expectancy for each.

        Args:
            trades: List of trade dicts as returned by simulate_symbol.

        Returns:
            Dict mapping score band label to a stats dict with keys: lo, hi,
            count, wins, losses, win_rate, avg_win, avg_loss,
            expectancy_pct, expectancy_1k, is_positive.
        """
        results = {}
        for lo, hi, label in Backtester.SCORE_BANDS:
            band = [t for t in trades if lo <= t["score"] < hi]
            if not band:
                continue
            wins   = [t for t in band if t["outcome"] == "win"]
            losses = [t for t in band if t["outcome"] in ("loss", "timeout")]
            wr     = len(wins) / len(band)
            avg_w  = float(np.mean([t["pnl_pct"] for t in wins]))   if wins   else 0.0
            avg_l  = float(np.mean([t["pnl_pct"] for t in losses])) if losses else 0.0
            # expectancy_pct = expected % return per trade
            # expectancy_1k  = expected $ return per $1,000 position
            exp_pct = wr * avg_w + (1 - wr) * avg_l
            results[label] = {
                "lo": lo, "hi": hi, "count": len(band),
                "wins": len(wins), "losses": len(losses),
                "win_rate": wr,
                "avg_win":  round(avg_w,   3),
                "avg_loss": round(avg_l,   3),
                "expectancy_pct": round(exp_pct,      3),
                "expectancy_1k":  round(exp_pct * 10, 2),
                "is_positive": exp_pct > 0,
            }
        return results

    def _recommend_threshold(self, analysis: dict) -> tuple[float, str]:
        """Find the lowest score band with positive expectancy and n >= 10.

        Args:
            analysis: Dict as returned by analyze_results.

        Returns:
            Tuple of (recommended_threshold, reason_string).
        """
        positive = [v for v in analysis.values() if v["is_positive"] and v["count"] >= 10]
        if not positive:
            return 10.0, "No band showed positive expectancy — review strategy entirely"

        best_lo  = min(v["lo"] for v in positive)
        current  = config.MIN_SIGNAL_SCORE_TO_AI

        if abs(best_lo - current) < 0.3:
            return current, f"Current threshold {current} is already well-calibrated — no change needed"

        direction = "Raise" if best_lo > current else "Lower"
        return best_lo, f"{direction} from {current} → {best_lo} to cut losing setups"

    def _build_report(self, analysis: dict, days: int,
                      total_trades: int, symbols: list[str]) -> str:
        """Build a formatted plain-text backtest report.

        Args:
            analysis: Dict as returned by analyze_results.
            days: Lookback window in calendar days.
            total_trades: Total number of simulated setups found.
            symbols: List of symbols that had sufficient data.

        Returns:
            Multi-line string report ready for printing or emailing.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        rec_threshold, rec_reason = self._recommend_threshold(analysis)
        sep = "─" * 62

        lines = [
            "╔══════════════════════════════════════════════════════════════╗",
            f"  WEEKLY BACKTEST REPORT — {today}",
            f"  Symbols : {', '.join(symbols[:8])}{'…' if len(symbols) > 8 else ''}",
            f"  Window  : last {days} calendar days  |  Total setups: {total_trades}",
            "╚══════════════════════════════════════════════════════════════╝",
            "",
            sep,
            "  SCORE BAND BREAKDOWN",
            sep,
            f"  {'Band':<24} {'n':>5}  {'Win%':>6}  {'AvgWin':>8}  {'AvgLoss':>9}  {'E/$1K':>7}  Edge",
            sep,
        ]

        for label, r in sorted(analysis.items(), key=lambda x: x[1]["lo"]):
            flag = "✓" if r["is_positive"] else "✗ LOSING"
            lines.append(
                f"  {label:<24} {r['count']:>5}  "
                f"{r['win_rate']:>5.0%}  "
                f"{r['avg_win']:>+7.2f}%  "
                f"{r['avg_loss']:>+8.2f}%  "
                f"{r['expectancy_1k']:>+6.2f}  "
                f"{flag}"
            )

        lines += [
            "",
            sep,
            "  RECOMMENDATION",
            sep,
            f"  {rec_reason}",
            f"",
            f"  → Set MIN_SIGNAL_SCORE_TO_AI = {rec_threshold}",
            f"  → File: config.py  (search: MIN_SIGNAL_SCORE_TO_AI)",
            f"  → Current value: {config.MIN_SIGNAL_SCORE_TO_AI}",
            "",
        ]

        if total_trades < 30:
            lines += [
                sep,
                "  ⚠  WARNING: LOW SAMPLE SIZE",
                sep,
                f"  Only {total_trades} setups found — recommendations may be unreliable.",
                "  Run again after more trading days have accumulated (aim for 100+).",
                "",
            ]

        lines += [
            sep,
            "  HOW TO APPLY",
            sep,
            "  1. Open config.py",
            f"  2. Find:   MIN_SIGNAL_SCORE_TO_AI = {config.MIN_SIGNAL_SCORE_TO_AI}",
            f"  3. Change: MIN_SIGNAL_SCORE_TO_AI = {rec_threshold}",
            "  4. Restart the bot",
            "",
            "  Note: This adjusts the pre-AI gate only. Claude still applies",
            "  its own judgment on every signal that passes this threshold.",
            sep,
        ]

        return "\n".join(lines)

    def _send_report_email(self, body: str, days: int) -> None:
        """Email the backtest report to the configured recipient.

        Args:
            body: Plain-text report body to send.
            days: Lookback window in calendar days (used in the subject line).
        """
        today   = datetime.now().strftime("%Y-%m-%d")
        subject = f"📊 Backtest Report {today} | {days}-day window"
        msg            = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = config.SMTP_USER
        msg["To"]      = config.RECIPIENT_EMAIL
        try:
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as s:
                s.ehlo()
                s.starttls()
                s.login(config.SMTP_USER, config.SMTP_PASS)
                s.send_message(msg)
            log.info("Backtest report emailed → %s", config.RECIPIENT_EMAIL)
        except Exception as e:
            log.error("Failed to email backtest report: %s", e)

    def run_backtest(self, days: int = 180, symbols: list[str] | None = None) -> None:
        """Fetch historical bars, simulate all symbols, analyse results, email report.

        Called by main.py weekly scheduler and by CLI.

        Args:
            days: Lookback window in calendar days. Defaults to 180.
            symbols: Override symbol list. Defaults to None (uses config.WATCHLIST).
        """
        if symbols is None:
            symbols = config.WATCHLIST

        log.info("Backtest starting — %d symbols, %d-day window", len(symbols), days)

        bars_map = self.broker.get_bars_multi(symbols, "5Min", days)
        if not bars_map:
            log.error("Backtest aborted — no historical bars returned")
            return

        all_trades:      list[dict] = []
        active_symbols:  list[str]  = []

        for sym in symbols:
            df = bars_map.get(sym)
            if df is None or df.empty or len(df) < Backtester.MIN_BARS_REQD:
                log.warning("Backtest: skipping %s — only %d bars",
                            sym, len(df) if df is not None else 0)
                continue
            active_symbols.append(sym)
            all_trades.extend(self.simulate_symbol(sym, df))

        log.info("Backtest complete — %d setups across %d symbols",
                 len(all_trades), len(active_symbols))

        if not all_trades:
            log.warning("Backtest: zero setups found — nothing to report")
            return

        analysis = self.analyze_results(all_trades)
        report   = self._build_report(analysis, days, len(all_trades), active_symbols)

        print(report)

        if config.SMTP_USER and config.SMTP_PASS:
            self._send_report_email(report, days)
        else:
            log.warning("Backtest report not emailed — SMTP_USER/SMTP_PASS not set in .env")


if __name__ == "__main__":
    from analysis.indicators import IndicatorEngine
    from analysis.signal_scorer import SignalScorer
    from core.broker import AlpacaBroker

    ap = argparse.ArgumentParser(description="Run signal-scorer backtest")
    ap.add_argument("--days",    type=int,  default=180,
                    help="Lookback window in calendar days (default: 180)")
    ap.add_argument("--symbols", nargs="*", default=None,
                    help="Override symbol list (default: config.WATCHLIST)")
    args = ap.parse_args()

    bt = Backtester(AlpacaBroker(), IndicatorEngine(), SignalScorer())
    bt.run_backtest(days=args.days, symbols=args.symbols)
