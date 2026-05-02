import json
import pathlib
from datetime import date, datetime
import sqlite3

import anthropic
import config
from core.database import log


from agents.study_data import StudyDataMixin

class MarketAnalyst(StudyDataMixin):
    STUDY_SYSTEM_PROMPT = (
        pathlib.Path(__file__).parent.parent / "prompts" / "morning_study.md"
    ).read_text()

    def __init__(self, broker, indicators, pre_market_client, yield_curve_client,
                 short_interest_client, dynamic_watchlist):
        """Args:
            broker: Alpaca broker (bars, snapshots, news).
            indicators: IndicatorEngine for studies.
            pre_market_client: Pre-market levels client.
            yield_curve_client: Macro yield client.
            short_interest_client: Short interest client.
            dynamic_watchlist: Persisted watchlist for news/carryover.
        """
        self.broker           = broker
        self.indicators       = indicators
        self.pre_market       = pre_market_client
        self.yc               = yield_curve_client
        self.si               = short_interest_client
        self.dynamic_watchlist = dynamic_watchlist
        self._client = anthropic.Anthropic(
            api_key=config.ANTHROPIC_API_KEY,
            timeout=120.0,
            max_retries=0,
        )

    def _save_daily_plan(self, plan: dict) -> None:
        """Persist plan to daily_plans for the plan's date (or today).

        Args:
            plan: Daily plan dict (must include or default date).
        """
        conn = sqlite3.connect(config.DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_plans (
                date  TEXT PRIMARY KEY,
                plan  TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT OR REPLACE INTO daily_plans (date, plan) VALUES (?,?)",
            (plan.get("date", date.today().isoformat()), json.dumps(plan)),
        )
        conn.commit()
        conn.close()

    def load_todays_plan(self) -> dict | None:
        """Load today's plan from SQLite if the morning study already completed.

        Returns:
            Plan dict, or None if missing.
        """
        conn = sqlite3.connect(config.DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_plans (
                date TEXT PRIMARY KEY,
                plan TEXT NOT NULL
            )
        """)
        row = conn.execute(
            "SELECT plan FROM daily_plans WHERE date=?",
            (date.today().isoformat(),),
        ).fetchone()
        conn.commit()
        conn.close()
        return json.loads(row[0]) if row else None

    def run_morning_study(self, account: dict) -> dict:
        """Build context, call the study model, enforce overrides, save plan.

        Args:
            account: settled_cash, total_equity, deployed_today, etc.

        Returns:
            Daily trading plan dict (bias, posture, candidates, warnings, ...).
        """
        log.info("=== MORNING STUDY START ===")

        market_ctx    = self._get_market_context()
        history       = self._get_full_history()
        missed_opps   = self._get_missed_opportunities()
        econ_calendar = self._get_economic_calendar()
        gappers, breadth = self._get_gap_and_breadth(config.WATCHLIST)

        log.info("Morning study: fetching bars for %d symbols…", len(config.WATCHLIST))
        bars_5m = self.broker.get_bars_multi(config.WATCHLIST, "5Min", days=3)
        log.info("Morning study: bars received for %d symbols", len(bars_5m))
        watchlist_data = []
        for sym, df in bars_5m.items():
            try:
                if len(df) < 25:
                    continue
                df  = self.indicators.compute_indicators(df)
                sig = self.indicators.get_signal_summary(df)
                if sig:
                    watchlist_data.append({
                        "symbol":     sym,
                        "bucket":     config.SYMBOL_BUCKET.get(sym, "unknown"),
                        "indicators": sig,
                    })
            except Exception as e:
                log.warning("Study watchlist error %s: %s", sym, e)
        log.info("Morning study: %d symbols with valid indicators", len(watchlist_data))

        # Overnight news — catalyst context (use dynamic watchlist: yesterday's pre-Claude survivors)
        _news_wl     = self.dynamic_watchlist.load()
        news_raw     = self.broker.get_news_headlines(_news_wl, hours_back=18)
        news_summary = {
            sym: [h["headline"] for h in articles[:3]]
            for sym, articles in news_raw.items()
        }
        log.info("Morning news: %d symbols have headlines", len(news_summary))

        # Pre-market gap analysis — establishes opening levels and gap candidates
        log.info("Morning study: fetching pre-market data…")
        pm_data = self.pre_market.get_premarket_data(config.WATCHLIST)
        log.info("Pre-market: %d/%d symbols have extended-hours data", len(pm_data), len(config.WATCHLIST))

        # Yield curve + credit spread — macro risk posture signal
        yc_data = self.yc.get_yield_curve()
        log.info("Yield curve: %s [%s]", yc_data.get("note", "unavailable"), yc_data.get("signal"))

        # Short interest — squeeze candidates and institutional conviction
        log.info("Morning study: fetching short interest (top 30 symbols)…")
        si_data = self.si.get_short_interest(config.WATCHLIST[:30])
        log.info("Short interest: %d symbols fetched", len(si_data))

        # Missed-opp split for prompt
        misses  = [m for m in missed_opps if m["was_miss"]]
        correct = [m for m in missed_opps if not m["was_miss"]]

        # ── Build prompt ──────────────────────────────────────────────────────────
        macro_flag   = econ_calendar.get("macro_flag", "none")
        macro_banner = ""
        if macro_flag == "stand_aside":
            macro_banner = "\n⚠️  MACRO ALERT: High-impact economic event today — risk_posture MUST be stand_aside.\n"
        elif macro_flag == "caution":
            macro_banner = "\n⚠️  MACRO CAUTION: Significant economic release today — be conservative.\n"

        user_content = f"""## TODAY: {date.today().isoformat()}
{macro_banner}
## ACCOUNT STATE
{json.dumps(account, indent=2)}

## MARKET BENCHMARKS (SPY / QQQ / Sectors)
{json.dumps(market_ctx, indent=2)}

## ECONOMIC CALENDAR (today's USD events)
macro_flag: {macro_flag}
{json.dumps(econ_calendar, indent=2)}

## GAP SCAN (pre-market movers ≥1% vs yesterday's close)
{len(gappers)} symbols gapping today.
{json.dumps(gappers[:20], indent=2)}

## MARKET BREADTH (watchlist advance/decline)
{json.dumps(breadth, indent=2)}

## WATCHLIST INDICATOR SCAN
{json.dumps(watchlist_data, indent=2)}

## OVERNIGHT NEWS (last 18 hours — catalyst context)
{len(news_summary)} of {len(config.WATCHLIST)} watchlist symbols have headlines.
Use this to identify positive catalysts (earnings beat, upgrade, partnership) that may
sustain momentum, and negative catalysts (miss, downgrade, guidance cut) to avoid.
No news is NEUTRAL — do not penalise symbols simply for having no headlines.
{json.dumps(news_summary, indent=2)[:2000]}

## PRE-MARKET LEVELS (4:00–9:30 AM ET extended-hours data)
Pre-market high/low become key intraday support/resistance for gap-and-go setups.
Gap-up: pm_high = first resistance; gap-down: pm_low = first support.
{json.dumps({s: {"gap_pct": d["gap_pct"], "direction": d["gap_direction"],
                 "pm_high": d["pm_high"], "pm_low": d["pm_low"],
                 "prev_close": d["prev_close"]}
             for s, d in pm_data.items()}, indent=2) if pm_data else "No pre-market data available."}

## MACRO CONDITIONS (Yield Curve + Credit Spreads)
yield_curve_signal: {yc_data.get("signal", "unknown")}
size_multiplier: ×{yc_data.get("size_multiplier", 1.0):.2f}
{yc_data.get("note", "data unavailable")}
Interpretation:
  "risk_off"  → inverted curve or credit stress — reduce size 25%, favour defensive setups
  "cautious"  → flattening curve — reduce size 15%, avoid high-beta momentum
  "normal"    → healthy spread — standard sizing rules apply
  "risk_on"   → steep positive curve — economy expanding; momentum setups preferred

## SHORT INTEREST (bi-monthly FINRA/NASDAQ data via Yahoo Finance)
Squeeze candidates: >20% float short + >10 days to cover → forced covering amplifies gains.
High short interest alone (without catalyst) is bearish — institutions are positioned for decline.
{json.dumps({s: {"short_pct": f"{d['short_pct_float']:.1%}", "days_to_cover": d["days_to_cover"],
                 "signal": d["signal"]}
             for s, d in si_data.items()}, indent=2) if si_data else "No short interest data available."}

## COMPLETE TRADING HISTORY
{json.dumps(history, indent=2)}

## YESTERDAY'S MISSED OPPORTUNITIES
Yesterday we SKIPPED {len(missed_opps)} symbols.
Of those, {len(misses)} rose >1% within 60 min of the skip (potential misses).
{len(correct)} fell or stayed flat (skips were correct).

Potential misses (skipped → price rose):
{json.dumps(misses[:10], indent=2)}

Correctly avoided (skipped → price fell or flat):
{json.dumps(correct[:5], indent=2)}

For each potential miss, the "skip_reason" field tells you WHICH condition blocked it.
Propose a threshold_override ONLY if you see a consistent pattern across ≥2 misses.
A single outlier is not sufficient evidence.

Conduct your full pre-market study now.
Apply the VIX regime, economic calendar, gap scan, and breadth rules as instructed.
Output the Daily Trading Plan JSON."""

        # ── Call Claude ───────────────────────────────────────────────────────────
        raw = ""
        try:
            resp = self._client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=3000,
                system=MarketAnalyst.STUDY_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            from anthropic.types import TextBlock
            raw = next((b.text for b in resp.content if isinstance(b, TextBlock)), "").strip()
            if raw.startswith("```"):
                parts = raw.split("```")
                raw   = parts[1] if len(parts) > 1 else raw
                if raw.startswith("json"):
                    raw = raw[4:]
            raw  = raw.strip()
            plan = json.loads(raw)
        except json.JSONDecodeError as e:
            log.error("Study returned invalid JSON: %s | raw=%s", e, raw[:400])
            plan = {
                "date":                        date.today().isoformat(),
                "market_bias":                 "unknown",
                "risk_posture":                "conservative",
                "macro_event_flag":            macro_flag,
                "market_summary":              "Study failed — defaulting to conservative posture.",
                "vix_proxy_note":              "Unknown",
                "breadth_summary":             "Unknown",
                "daily_profit_target_dollars": 50,
                "daily_max_loss_dollars":      200,
                "sectors_to_favour":           [],
                "sectors_to_avoid":            [],
                "setups_to_use":               [],
                "setups_to_avoid":             [],
                "top_candidates":              [],
                "history_lessons":             [],
                "special_warnings":            ["Morning study failed — trade with extreme caution"],
            }
        except Exception as e:
            log.error("Morning study failed: %s", e)
            plan = {
                "date":               date.today().isoformat(),
                "risk_posture":       "stand_aside",
                "macro_event_flag":   macro_flag,
                "market_bias":        "unknown",
                "top_candidates":     [],
                "daily_profit_target_dollars": 0,
                "daily_max_loss_dollars":      200,
                "special_warnings":   [str(e)],
            }

        # ── Hard overrides (safety net — cannot be bypassed by Claude output) ─────
        if econ_calendar.get("is_fomc_day"):
            if plan.get("risk_posture") != "stand_aside":
                log.warning("FOMC day: overriding Claude's risk_posture to stand_aside")
                plan["risk_posture"] = "stand_aside"
            plan.setdefault("special_warnings", []).insert(
                0, "FOMC DAY — stand_aside enforced by economic calendar guard"
            )
            plan["macro_event_flag"] = "stand_aside"

        elif econ_calendar.get("has_critical_event") and plan.get("risk_posture") == "aggressive":
            log.info("Critical macro event: downgrading aggressive → conservative")
            plan["risk_posture"]     = "conservative"
            plan["macro_event_flag"] = "caution"

        # Stamp breadth condition into plan for downstream reference
        plan["breadth_condition"] = breadth.get("breadth_condition", "UNKNOWN")
        plan.setdefault("macro_event_flag", macro_flag)

        # ── Persist and log ───────────────────────────────────────────────────────
        self._save_daily_plan(plan)
        log.info("=== MORNING STUDY COMPLETE ===")
        log.info(
            "Bias=%-8s  Posture=%-12s  Candidates=%d  Target=$%s  Breadth=%s  Macro=%s",
            plan.get("market_bias"), plan.get("risk_posture"),
            len(plan.get("top_candidates", [])),
            plan.get("daily_profit_target_dollars"),
            plan.get("breadth_condition"),
            plan.get("macro_event_flag"),
        )
        if gappers:
            log.info(
                "Top gappers: %s",
                ", ".join(f"{g['symbol']} {g['change_pct']:+.1f}%" for g in gappers[:5]),
            )
        if plan.get("threshold_overrides"):
            log.info("  THRESHOLD OVERRIDES proposed: %s", plan["threshold_overrides"])
            if plan.get("threshold_rationale"):
                log.info("  OVERRIDE RATIONALE: %s", plan["threshold_rationale"])
        for lesson in plan.get("history_lessons", []):
            log.info("  LESSON: %s", lesson)
        for w in plan.get("special_warnings", []):
            log.warning("  WARNING: %s", w)

        return plan
