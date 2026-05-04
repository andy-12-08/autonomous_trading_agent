import json
import sqlite3
from datetime import date, timedelta

import config
from risk.expectancy import ExpectancyEngine


class ReviewLog:
    def __init__(self, db_path: str):
        """Args:
            db_path: Path to trading_log.db (or equivalent).
        """
        self.db_path = db_path
        self.SEP     = "─" * 72
        self.SEP2    = "═" * 72

    def section(self, title: str) -> None:
        """Args:
            title: Heading text.

        Returns:
            None.
        """
        print(f"\n{self.SEP2}")
        print(f"  {title}")
        print(self.SEP2)

    def run(self) -> None:
        """Print expectancy, history, decisions, and guard status to stdout.

        Returns:
            None.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row

        all_decisions = [dict(r) for r in conn.execute(
            "SELECT * FROM decisions ORDER BY ts DESC LIMIT 1000"
        ).fetchall()]

        exp = ExpectancyEngine(self.db_path)

        self.section("ALL-TIME EXPECTANCY")
        overall = exp.compute_expectancy(all_decisions)
        if overall:
            sign   = "+" if overall["is_positive"] else ""
            status = "✓ POSITIVE EDGE" if overall["is_positive"] else "✗ NEGATIVE EDGE — REVIEW STRATEGY"
            print(f"  Expectancy : {sign}${overall['expectancy']:.2f} per trade  [{status}]")
            print(f"  Win rate   : {overall['win_rate']:.0%}")
            print(f"  Avg win    : ${overall['avg_win']:.2f}")
            print(f"  Avg loss   : ${overall['avg_loss']:.2f}")
            print(f"  Sample     : {overall['total_trades']} closed trades")
        else:
            print("  Insufficient data (need ≥ 10 closed trades)")

        self.section("SETUP-TYPE EXPECTANCY BREAKDOWN")
        by_setup = exp.compute_expectancy_by_setup(all_decisions, min_sample=2)
        if by_setup:
            print(f"  {'Setup Type':28s}  {'E$/trade':>8}  {'WR':>5}  {'AvgW':>7}  {'AvgL':>7}  {'n':>4}  Status")
            print(f"  {self.SEP}")
            for st, st_exp in sorted(by_setup.items(),
                                      key=lambda x: x[1]["expectancy"], reverse=True):
                sign = "+" if st_exp["is_positive"] else ""
                flag = "✓" if st_exp["is_positive"] else "✗ SUPPRESS"
                print(f"  {st[:28]:28s}  {sign}${st_exp['expectancy']:>6.2f}  "
                      f"{st_exp['win_rate']:>4.0%}  "
                      f"${st_exp['avg_win']:>5.0f}  ${st_exp['avg_loss']:>5.0f}  "
                      f"{st_exp['total_trades']:>4}  {flag}")
        else:
            print("  Insufficient data per setup type (need ≥ 2 trades per type)")

        self.section("DAILY PERFORMANCE HISTORY")
        daily_rows = list(conn.execute(
            "SELECT * FROM daily_summary ORDER BY date DESC LIMIT 30"
        ).fetchall())

        if daily_rows:
            print(f"  {'Date':12s}  {'Trades':>6}  {'W':>4}  {'L':>4}  {'WR':>5}  {'Net P&L':>9}  Notes")
            print(f"  {self.SEP}")
            total_pnl = 0.0
            for row in reversed(daily_rows):
                r  = dict(row)
                wr = r["wins"] / r["trades"] if r["trades"] else 0
                net = r["net_pnl"] or 0
                total_pnl += net
                flag = "✓" if net >= 0 else "✗"
                print(f"  {r['date']:12s}  {r['trades']:>6}  {r['wins']:>4}  {r['losses']:>4}  "
                      f"{wr:>4.0%}  ${net:>8.2f}  {flag}")
            print(f"  {self.SEP}")
            print(f"  {'TOTAL':12s}  {'':6}  {'':4}  {'':4}  {'':5}  ${total_pnl:>8.2f}")
        else:
            print("  No daily summaries recorded yet.")

        self.section("RECENT DECISIONS (last 60)")
        recent = conn.execute(
            "SELECT * FROM decisions ORDER BY ts DESC LIMIT 60"
        ).fetchall()

        for row in recent:
            r       = dict(row)
            pnl_str = f"${r['pnl']:+.2f}" if r.get("pnl") is not None else "    n/a"
            setup   = r.get("setup_type") or "—"
            score   = f"  score={r['signal_score']:.1f}" if r.get("signal_score") is not None else ""
            veto    = f"  [{r['veto_rule']}]" if r.get("veto_rule") else ""
            print(f"  [{r['ts'][:19]}] {r['action']:14s} {r['symbol']:6s} "
                  f"@ ${r.get('price') or 0:>8.2f}  qty={r.get('qty') or 0:>5.0f}  "
                  f"pnl={pnl_str:>9}  [{setup}]{score}{veto}")
            reason = (r.get("reasoning") or "")[:100]
            if reason:
                print(f"    → {reason}")

        self.section("YESTERDAY'S MISSED TRADES — SKIP BREAKDOWN")
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        skip_rows = conn.execute(
            """SELECT symbol, ts, signal_score, veto_rule, reasoning
               FROM decisions
               WHERE ts LIKE ? AND action = 'SKIP' AND signal_score IS NOT NULL
               ORDER BY signal_score DESC""",
            (f"{yesterday}%",)
        ).fetchall()

        if skip_rows:
            print(f"  {'Symbol':6s}  {'Score':>5}  {'Veto Rule':20s}  Reason")
            print(f"  {self.SEP}")
            for row in skip_rows:
                r = dict(row)
                sc  = f"{r['signal_score']:.1f}" if r.get("signal_score") is not None else " n/a"
                vr  = (r.get("veto_rule") or "—")[:20]
                rsn = (r.get("reasoning") or "")[:70]
                print(f"  {r['symbol']:6s}  {sc:>5}  {vr:20s}  {rsn}")
        else:
            print("  No scored skips recorded yesterday (bot may not have run yet).")

        self.section("OPEN POSITIONS (DB)")
        positions = conn.execute("SELECT * FROM positions").fetchall()
        if positions:
            for row in positions:
                r = dict(row)
                print(f"  {r['symbol']:6s}  entry=${r['entry_price']:.2f}  qty={r['qty']:.0f}  "
                      f"SL=${r['stop_loss']:.2f}  TP=${r['take_profit']:.2f}  "
                      f"trail={'yes' if r['trailing'] else 'no'}  "
                      f"partial={'yes' if r.get('partial_taken') else 'no'}")
        else:
            print("  No open positions.")

        self.section("GUARDS STATUS (today's session)")
        today = date.today().isoformat()
        plan_row = conn.execute(
            "SELECT plan FROM daily_plans WHERE date=?", (today,)
        ).fetchone()
        if plan_row:
            plan = json.loads(plan_row["plan"])
            print(f"  Market bias   : {plan.get('market_bias', 'N/A')}")
            print(f"  Risk posture  : {plan.get('risk_posture', 'N/A')}")
            print(f"  Summary       : {(plan.get('market_summary') or '')[:120]}")
            for w in plan.get("special_warnings", []):
                print(f"  ⚠ WARNING     : {w}")
            for lesson in plan.get("history_lessons", []):
                print(f"  📖 LESSON     : {lesson}")
        else:
            print("  No daily plan for today (morning study hasn't run yet).")

        conn.close()
        print(f"\n{self.SEP2}\n")


if __name__ == "__main__":
    ReviewLog(config.DB_PATH).run()
