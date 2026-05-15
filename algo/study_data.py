import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

import config
from core.database import log


class StudyDataMixin:
    def _get_market_context(self) -> dict:
        """Pull indicators for SPY, QQQ, and sector proxies.

        Returns:
            Dict mapping benchmark symbol to its signal summary dict.
            Symbols that fail are omitted silently.
        """
        benchmarks = ["SPY", "QQQ", "UVXY", "XLK", "XLF", "XLE", "XLV", "XLY"]
        context    = {}
        for sym in benchmarks:
            try:
                df = self.broker.get_bars(sym, "5Min", days=3)
                if df.empty or len(df) < 25:
                    continue
                df  = self.indicators.compute_indicators(df)
                sig = self.indicators.get_signal_summary(df)
                if len(df) >= 2:
                    prev_close = float(df["close"].iloc[-77]) if len(df) > 77 else float(df["close"].iloc[0])
                    last_close = float(df["close"].iloc[-1])
                    sig["day_change_pct"] = round((last_close - prev_close) / prev_close * 100, 2)
                context[sym] = sig
            except Exception as e:
                log.warning("Market context failed for %s: %s", sym, e)
        return context

    def _get_economic_calendar(self) -> dict:
        """Fetch today's high-impact USD economic events from ForexFactory.

        Returns a summary dict with macro_flag in {none, caution, stand_aside}.
        Falls back gracefully if the feed is unreachable.

        Returns:
            Dict with keys: high_impact, medium_impact, has_critical_event,
            is_fomc_day, macro_flag, source.
        """
        today = datetime.now(config.ET).date()

        FOMC_KEYWORDS     = {"fomc", "federal reserve", "interest rate decision", "fed rate", "monetary policy"}
        CRITICAL_KEYWORDS = FOMC_KEYWORDS | {
            "cpi", "consumer price index", "pce", "personal consumption expenditure",
            "nfp", "non-farm payroll", "payroll", "unemployment rate",
            "gdp", "gross domestic product",
        }

        try:
            resp = requests.get(
                "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                timeout=8,
                headers={"User-Agent": "TradingBot/1.0"},
            )
            resp.raise_for_status()
            events = resp.json()

            today_usd: list[dict] = []
            for e in events:
                if e.get("country") != "USD":
                    continue
                try:
                    evt_dt = datetime.fromisoformat(e.get("date", ""))
                    if evt_dt.date() != today:
                        continue
                    today_usd.append({
                        "title":    e.get("title", ""),
                        "time":     evt_dt.astimezone(config.ET).strftime("%H:%M ET"),
                        "impact":   e.get("impact", ""),
                        "forecast": e.get("forecast") or "n/a",
                        "previous": e.get("previous") or "n/a",
                    })
                except (ValueError, TypeError):
                    continue

            high_impact   = [e for e in today_usd if e["impact"] == "High"]
            medium_impact = [e for e in today_usd if e["impact"] == "Medium"]

            title_tokens = {tok for e in high_impact for tok in e["title"].lower().split()}
            is_fomc      = bool(FOMC_KEYWORDS & title_tokens)
            has_critical = is_fomc or any(
                kw in e["title"].lower() for e in high_impact for kw in CRITICAL_KEYWORDS
            )

            # FOMC only → stand_aside.  CPI/NFP/GDP print at 8:30 AM and are
            # already priced in by the open — trade the post-data trend instead.
            # Multiple high-impact events on the same day (e.g. CPI m/m + Core CPI)
            # do NOT escalate beyond caution; only FOMC warrants a full block.
            macro_flag = "stand_aside" if is_fomc else ("caution" if has_critical else "none")

            log.info(
                "Economic calendar: %d high / %d medium USD events | flag=%s",
                len(high_impact), len(medium_impact), macro_flag,
            )
            return {
                "high_impact":        high_impact,
                "medium_impact":      medium_impact[:5],
                "has_critical_event": has_critical,
                "is_fomc_day":        is_fomc,
                "macro_flag":         macro_flag,
                "source":             "forexfactory",
            }

        except Exception as e:
            log.warning("Economic calendar fetch failed: %s — assuming normal macro environment", e)
            return {
                "high_impact":        [],
                "medium_impact":      [],
                "has_critical_event": False,
                "is_fomc_day":        False,
                "macro_flag":         "none",
                "source":             "unavailable",
            }

    def _get_gap_and_breadth(self, watchlist: list[str]) -> tuple[list[dict], dict]:
        """Perform a single snapshot sweep of the watchlist for gaps and breadth.

        Args:
            watchlist: List of ticker symbols to scan.

        Returns:
            Tuple of (gappers, breadth) where gappers is a list of dicts for
            stocks with >= 1% pre-market move, sorted by magnitude descending,
            and breadth is a dict with advance/decline stats and sector rotation.
        """
        snapshots = self.broker.get_snapshots_bulk(watchlist)
        if not snapshots:
            log.warning("Gap/breadth: no snapshot data — skipping")
            return [], {"breadth_condition": "UNKNOWN", "total_symbols": 0}

        # ── Gap scan ──────────────────────────────────────────────────────────────
        gappers: list[dict] = []
        for sym, snap in snapshots.items():
            chg = snap.get("change_pct", 0)
            if abs(chg) < 1.0:
                continue
            gappers.append({
                "symbol":     sym,
                "price":      snap.get("price"),
                "change_pct": round(chg, 2),
                "direction":  "UP" if chg > 0 else "DOWN",
                "strength": (
                    "STRONG_UP"   if chg >=  2.0 else
                    "MODERATE_UP" if chg >=  1.0 else
                    "STRONG_DN"   if chg <= -2.0 else
                    "MODERATE_DN"
                ),
                "bucket": config.SYMBOL_BUCKET.get(sym, "unknown"),
            })
        gappers.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
        log.info("Gap scan: %d symbols with >= 1%% pre-market gap", len(gappers))

        # ── Market breadth ────────────────────────────────────────────────────────
        changes = [(sym, snap.get("change_pct", 0)) for sym, snap in snapshots.items()]
        vals    = [c for _, c in changes]

        advancing = sum(1 for c in vals if c > 0)
        declining = sum(1 for c in vals if c < 0)
        unchanged = len(vals) - advancing - declining
        ad_ratio  = round(advancing / max(declining, 1), 2)
        avg_chg   = round(sum(vals) / len(vals), 2) if vals else 0.0

        sector_map: dict[str, list[float]] = {}
        for sym, chg in changes:
            bucket = config.SYMBOL_BUCKET.get(sym, "unknown")
            sector_map.setdefault(bucket, []).append(chg)
        sector_avg = {
            s: round(sum(cs) / len(cs), 2)
            for s, cs in sector_map.items() if cs
        }
        # Identify leading and lagging sectors
        sorted_sectors = sorted(sector_avg.items(), key=lambda x: x[1], reverse=True)
        leading_sectors = [s for s, _ in sorted_sectors[:3]]
        lagging_sectors = [s for s, _ in sorted_sectors[-3:] if sorted_sectors[-3:]]

        breadth_condition = (
            "BROAD_RALLY"   if ad_ratio >= 3.0 and avg_chg >  0.5 else
            "MILD_RALLY"    if advancing > declining                 else
            "BROAD_SELLOFF" if ad_ratio <= 0.33 and avg_chg < -0.5  else
            "MILD_SELLOFF"  if declining > advancing                  else
            "MIXED"
        )

        breadth = {
            "total_symbols":     len(vals),
            "advancing":         advancing,
            "declining":         declining,
            "unchanged":         unchanged,
            "ad_ratio":          ad_ratio,
            "avg_change_pct":    avg_chg,
            "strong_up_2pct":    sum(1 for c in vals if c >  2.0),
            "strong_dn_2pct":    sum(1 for c in vals if c < -2.0),
            "sector_avg_change": sector_avg,
            "leading_sectors":   leading_sectors,
            "lagging_sectors":   lagging_sectors,
            "breadth_condition": breadth_condition,
        }
        log.info(
            "Breadth: %d adv / %d dec | A/D=%.2f | avg=%.2f%% | %s | leading=%s",
            advancing, declining, ad_ratio, avg_chg, breadth_condition, leading_sectors,
        )
        return gappers, breadth

    def _get_full_history(self) -> dict:
        """Return complete decision history plus performance statistics.

        Returns:
            Dict with keys: recent_decisions (last 50), daily_summaries (last 10),
            symbol_performance (wins/losses/total_pnl per symbol), total_trades,
            total_pnl.
        """
        conn = sqlite3.connect(config.DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            all_decisions = [dict(r) for r in conn.execute(
                "SELECT * FROM decisions ORDER BY ts DESC LIMIT 200"
            ).fetchall()]

            daily_summaries = [dict(r) for r in conn.execute(
                "SELECT * FROM daily_summary ORDER BY date DESC LIMIT 10"
            ).fetchall()]

            setup_stats: dict[str, dict] = {}
            for d in all_decisions:
                pnl    = d.get("pnl") or 0
                action = d.get("action", "")
                if action not in ("BUY", "SELL", "PARTIAL_SELL"):
                    continue
                sym = d.get("symbol", "")
                if sym not in setup_stats:
                    setup_stats[sym] = {"wins": 0, "losses": 0, "total_pnl": 0}
                if pnl > 0:
                    setup_stats[sym]["wins"] += 1
                elif pnl < 0:
                    setup_stats[sym]["losses"] += 1
                setup_stats[sym]["total_pnl"] += pnl
        finally:
            conn.close()

        return {
            "recent_decisions":   all_decisions[:50],
            "daily_summaries":    daily_summaries,
            "symbol_performance": setup_stats,
            "total_trades":       len([d for d in all_decisions if d.get("action") in ("BUY", "SELL", "PARTIAL_SELL")]),
            "total_pnl":          sum((d.get("pnl") or 0) for d in all_decisions),
        }

    def _get_missed_opportunities(self) -> list[dict]:
        """Analyse yesterday's SKIP decisions against subsequent price action.

        Looks at yesterday's SKIP decisions and checks what price did in the
        60 minutes after each skip. Returns significant moves so the morning
        study can assess whether thresholds are too tight.

        Returns:
            List of dicts (sorted by max_gain_pct descending) for symbols that
            moved >= 0.5% after being skipped. Each dict contains symbol,
            skip_time, skip_price, max_gain_pct, max_loss_pct, net_60min_pct,
            was_miss, skip_reason, signal_score, veto_rule.
        """
        yesterday = (datetime.now(config.ET).date() - timedelta(days=1)).isoformat()

        conn = sqlite3.connect(config.DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            skips = [dict(r) for r in conn.execute(
                """SELECT symbol, ts, reasoning, signal_score, veto_rule FROM decisions
                   WHERE ts LIKE ? AND action = 'SKIP'
                   ORDER BY ts""",
                (f"{yesterday}%",)
            ).fetchall()]
        finally:
            conn.close()

        if not skips:
            return []

        first_skip: dict[str, dict] = {}
        for s in skips:
            sym = s["symbol"]
            if sym not in first_skip:
                first_skip[sym] = s

        utc_tz = timezone.utc
        result = []

        for sym, skip in list(first_skip.items())[:25]:
            try:
                df = self.broker.get_bars(sym, "5Min", days=2)
                if df.empty or len(df) < 5:
                    continue

                dti = pd.DatetimeIndex(df.index)
                df.index = dti.tz_localize("UTC") if dti.tz is None else dti

                skip_dt = datetime.fromisoformat(skip["ts"]).replace(tzinfo=utc_tz)
                future  = df[df.index >= skip_dt]
                if len(future) < 3:
                    continue

                skip_price  = float(future.iloc[0]["close"])
                lookahead   = future.iloc[:12]
                max_price   = float(lookahead["close"].max())
                min_price   = float(lookahead["close"].min())
                final_price = float(lookahead.iloc[-1]["close"])
                max_gain    = (max_price  - skip_price) / skip_price * 100
                max_loss    = (min_price  - skip_price) / skip_price * 100
                net_move    = (final_price - skip_price) / skip_price * 100

                if abs(max_gain) < 0.5 and abs(max_loss) < 0.5:
                    continue

                result.append({
                    "symbol":        sym,
                    "skip_time":     skip_dt.astimezone(config.ET).strftime("%H:%M ET"),
                    "skip_price":    round(skip_price,  2),
                    "max_gain_pct":  round(max_gain,    2),
                    "max_loss_pct":  round(max_loss,    2),
                    "net_60min_pct": round(net_move,    2),
                    "was_miss":      max_gain > 1.0,
                    "skip_reason":   (skip.get("reasoning") or "")[:200],
                    "signal_score":  skip.get("signal_score"),
                    "veto_rule":     skip.get("veto_rule"),
                })
            except Exception as e:
                log.warning("Missed-opp analysis %s: %s", sym, e)

        result.sort(key=lambda x: x["max_gain_pct"], reverse=True)
        missed  = [r for r in result if r["was_miss"]]
        correct = [r for r in result if not r["was_miss"]]
        log.info(
            "Missed-opp: %d skips → %d misses, %d correctly avoided",
            len(first_skip), len(missed), len(correct),
        )
        return result

