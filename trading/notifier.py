import smtplib
import sqlite3
import json
import threading
import time
from email.mime.text import MIMEText
from datetime import datetime

import config
from core.database import log


class Notifier:
    """Format and send daily summaries and per-trade SMTP alerts in background threads."""

    def __init__(self, config_module, db_path: str, expectancy_engine):
        """Store config, database path, and expectancy helper used in email bodies.

        Args:
            config_module: Module providing SMTP_HOST, SMTP_USER, RECIPIENT_EMAIL, LOG_FILE, etc.
            db_path: SQLite path for loading decisions and summaries.
            expectancy_engine: ExpectancyEngine instance for rollups in the daily email.
        """
        self.config     = config_module
        self.db_path    = db_path
        self.expectancy = expectancy_engine

    def _load_today_data(self) -> dict:
        """Load today's decisions, summary, plan, and open positions from the DB.

        Returns:
            Keys: today, today_decisions, all_decisions, summary, plan, open_positions.
        """
        today = datetime.now(config.ET).date().isoformat()
        conn  = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row

        today_decisions = [dict(r) for r in conn.execute(
            "SELECT * FROM decisions WHERE ts LIKE ? ORDER BY ts",
            (f"{today}%",)
        ).fetchall()]

        all_decisions = [dict(r) for r in conn.execute(
            "SELECT * FROM decisions ORDER BY ts DESC LIMIT 500"
        ).fetchall()]

        summary_row = conn.execute(
            "SELECT * FROM daily_summary WHERE date=?", (today,)
        ).fetchone()
        summary = dict(summary_row) if summary_row else {}

        plan_row = conn.execute(
            "SELECT plan FROM daily_plans WHERE date=?", (today,)
        ).fetchone()
        plan = json.loads(plan_row["plan"]) if plan_row else {}

        open_pos = [dict(r) for r in conn.execute("SELECT * FROM positions").fetchall()]
        conn.close()

        return {
            "today":            today,
            "today_decisions":  today_decisions,
            "all_decisions":    all_decisions,
            "summary":          summary,
            "plan":             plan,
            "open_positions":   open_pos,
        }

    def _build_body(self, data: dict) -> str:
        """Format the plaintext body for the daily summary email.

        Args:
            data: Output of _load_today_data.

        Returns:
            Email body string.
        """
        s   = data["summary"]
        p   = data["plan"]
        dec = data["today_decisions"]
        all_dec = data["all_decisions"]

        net_pnl   = s.get("net_pnl",  0)
        trades    = s.get("trades",   0)
        wins      = s.get("wins",     0)
        losses    = s.get("losses",   0)
        win_rate  = wins / trades if trades else 0

        overall_exp = self.expectancy.compute_expectancy(all_dec)
        by_setup    = self.expectancy.compute_expectancy_by_setup(all_dec)

        status_line = "PROFITABLE DAY" if net_pnl >= 0 else "LOSS DAY"

        sep = "-" * 54

        lines = [
            f"+------------------------------------------------------+",
            f"  AUTONOMOUS TRADING BOT  DAILY REPORT",
            f"  {data['today']}",
            f"+------------------------------------------------------+",
            f"  {status_line}",
            "",
            sep,
            "  TODAY'S PERFORMANCE",
            sep,
            f"  Net P&L     : ${net_pnl:+.2f}",
            f"  Trades      : {trades}",
            f"  Wins        : {wins}",
            f"  Losses      : {losses}",
            f"  Win rate    : {win_rate:.0%}",
            "",
        ]

        if p:
            lines += [
                sep,
                "  TODAY'S MARKET CONTEXT (from morning study)",
                sep,
                f"  Bias        : {p.get('market_bias', 'N/A')}",
                f"  Posture     : {p.get('risk_posture', 'N/A')}",
                f"  Summary     : {p.get('market_summary', 'N/A')[:120]}",
                f"  Day target  : ${p.get('daily_profit_target_dollars', 'N/A')}",
                f"  Max loss    : ${p.get('daily_max_loss_dollars', 'N/A')}",
                "",
            ]

        if overall_exp:
            sign   = "+" if overall_exp["is_positive"] else ""
            status = "POSITIVE EDGE" if overall_exp["is_positive"] else "NEGATIVE EDGE - REVIEW STRATEGY"
            lines += [
                sep,
                "  ALL-TIME EXPECTANCY",
                sep,
                f"  Expectancy  : {sign}${overall_exp['expectancy']:.2f} per trade",
                f"  Win rate    : {overall_exp['win_rate']:.0%}",
                f"  Avg win     : ${overall_exp['avg_win']:.2f}",
                f"  Avg loss    : ${overall_exp['avg_loss']:.2f}",
                f"  Total trades: {overall_exp['total_trades']}",
                f"  Status      : {status}",
                "",
            ]

        if by_setup:
            lines += [sep, "  SETUP-TYPE EXPECTANCY (all-time)", sep]
            for st, exp in sorted(by_setup.items(),
                                   key=lambda x: x[1]["expectancy"], reverse=True):
                sign = "+" if exp["is_positive"] else ""
                flag = "OK" if exp["is_positive"] else "SUPPRESS"
                lines.append(
                    f"  {st[:24]:24s} | E={sign}${exp['expectancy']:5.2f} "
                    f"WR={exp['win_rate']:.0%} "
                    f"n={exp['total_trades']} {flag}"
                )
            lines.append("")

        if p.get("history_lessons"):
            lines += [sep, "  HISTORY LESSONS APPLIED TODAY", sep]
            for lesson in p["history_lessons"]:
                lines.append(f"   {lesson}")
            lines.append("")

        if p.get("special_warnings"):
            lines += [sep, "  WARNINGS FLAGGED", sep]
            for w in p["special_warnings"]:
                lines.append(f"  - {w}")
            lines.append("")

        executed = [d for d in dec if d.get("action") in ("BUY", "SELL", "PARTIAL_SELL")]
        if executed:
            lines += [sep, "  TODAY'S TRADE LOG", sep]
            for d in executed:
                pnl_str = f"${d['pnl']:+.2f}" if d.get("pnl") is not None else "  open"
                setup   = d.get("setup_type") or ""
                lines.append(
                    f"  {d['ts'][11:19]} {d['action']:12s} {d['symbol']:6s} "
                    f"@ ${d.get('price') or 0:.2f}  pnl={pnl_str:>8}  [{setup}]"
                )
            lines.append("")

        drift = self.expectancy.get_confidence_drift()
        if drift:
            lines += [
                sep,
                "  CONFIDENCE DRIFT DETECTED",
                sep,
                f"  Recent avg  : {drift['recent_avg']}/10  (last 7 days, n={drift['recent_n']})",
                f"  Baseline    : {drift['baseline_avg']}/10  (90-day avg, n={drift['baseline_n']})",
                f"  Drift       : {drift['drift']:+.1f} pts  ({drift['direction']})",
                f"  Action      : Review bot.log and recent decisions for confidence drift.",
                "",
            ]

        lines += [
            sep,
            f"  Bot running. Next session: next trading day at 09:30 ET.",
            f"  Log file: {self.config.LOG_FILE}  |  DB: {self.db_path}",
            "",
        ]

        return "\n".join(lines)

    def _send_email_async(self, msg: MIMEText, success_context: str, failure_context: str) -> None:
        """Send an email in a background thread with SSL and STARTTLS fallback.

        Args:
            msg: Fully prepared MIME message.
            success_context: Short log description for successful sends.
            failure_context: Short log description for failed sends.

        Returns:
            None.
        """
        def _send():
            """Run the SMTP retry loop for the prepared message.

            Returns:
                None.
            """
            for attempt in range(3):
                if attempt:
                    time.sleep(2 ** attempt)
                try:
                    with smtplib.SMTP_SSL(self.config.SMTP_HOST, 465, timeout=10) as server:
                        server.login(self.config.SMTP_USER, self.config.SMTP_PASS)
                        server.send_message(msg)
                    log.info("%s sent to %s", success_context, self.config.RECIPIENT_EMAIL)
                    return
                except Exception:
                    try:
                        with smtplib.SMTP(self.config.SMTP_HOST, 587, timeout=10) as server:
                            server.ehlo()
                            server.starttls()
                            server.login(self.config.SMTP_USER, self.config.SMTP_PASS)
                            server.send_message(msg)
                        log.info("%s sent via STARTTLS to %s", success_context, self.config.RECIPIENT_EMAIL)
                        return
                    except Exception as e2:
                        log.warning("%s attempt %d/3 failed: %s", failure_context, attempt + 1, e2)
            log.error("%s failed after 3 attempts", failure_context)

        threading.Thread(target=_send, daemon=True).start()

    def send_trade_alert(
        self,
        action:         str,
        symbol:         str,
        price:          float,
        qty:            float,
        equity:         float,
        daily_pnl:      float,
        deployed:       float = 0,
        positions_open: int   = 0,
        stop_loss:      float | None = None,
        take_profit:    float | None = None,
        pnl:            float | None = None,
        setup_type:     str   | None = None,
        reason:         str         = "",
    ) -> None:
        """Email a single trade alert (non-blocking daemon thread). No-op if SMTP unset.

        Args:
            action: BUY, SELL, or PARTIAL_SELL.
            symbol: Ticker.
            price: Execution or decision price.
            qty: Shares.
            equity: Account equity snapshot.
            daily_pnl: Today's P&L after the trade.
            deployed: Capital deployed today.
            positions_open: Open position count after trade.
            stop_loss: Stop price for BUY alerts.
            take_profit: Target for BUY alerts.
            pnl: Realized P&L for exits.
            setup_type: Strategy label.
            reason: Short rationale.

        Returns:
            None.
        """
        if not self.config.SMTP_USER or not self.config.SMTP_PASS:
            return

        now_et = datetime.now(config.ET).strftime("%Y-%m-%d %H:%M:%S ET")

        sep  = "-" * 54
        cost = price * qty

        if action == "BUY":
            risk_dollars = (price - stop_loss) * qty if stop_loss else 0
            subject = f"BUY {symbol}  {qty:.0f} @ ${price:.2f} | risk ${risk_dollars:.0f}"
        elif pnl is not None and pnl >= 0:
            pnl_pct = pnl / cost * 100 if cost else 0
            prefix  = "PARTIAL SELL" if action == "PARTIAL_SELL" else "SELL"
            subject = f"{prefix} {symbol}  +${pnl:.2f} (+{pnl_pct:.1f}%)"
        else:
            pnl_pct = (pnl / cost * 100) if (pnl and cost) else 0
            subject = f"SELL {symbol}  ${pnl:.2f} ({pnl_pct:.1f}%)" if pnl is not None \
                      else f"{action} {symbol} @ ${price:.2f}"

        lines = [
            f"+------------------------------------------------------+",
            f"  TRADE ALERT  {action}",
            f"  {symbol}  |  {now_et}",
            f"+------------------------------------------------------+",
            "",
            sep,
            f"  TRADE DETAILS",
            sep,
            f"  Action      : {action}",
            f"  Symbol      : {symbol}",
            f"  Shares      : {qty:.0f}",
            f"  Price       : ${price:.2f}",
            f"  Trade value : ${cost:.2f}",
        ]

        if action == "BUY":
            if stop_loss:
                risk_d = (price - stop_loss) * qty
                lines.append(f"  Stop loss   : ${stop_loss:.2f}   (risk ${risk_d:.2f})")
            if take_profit and stop_loss:
                reward  = (take_profit - price) * qty
                risk_d  = (price - stop_loss) * qty
                rr      = reward / risk_d if risk_d else 0
                lines.append(f"  Take profit : ${take_profit:.2f}   (target ${reward:.2f}  R:R {rr:.1f})")
            if setup_type:
                lines.append(f"  Setup       : {setup_type}")

        if pnl is not None:
            pnl_pct = pnl / cost * 100 if cost else 0
            lines.append(f"  Trade P&L   : ${pnl:+.2f}  ({pnl_pct:+.1f}%)")

        if reason:
            lines.append(f"  Reason      : {reason[:100]}")

        lines += [
            "",
            sep,
            "  ACCOUNT SNAPSHOT",
            sep,
            f"  Total equity  : ${equity:,.2f}",
            f"  Today's P&L   : ${daily_pnl:+.2f}",
            f"  Deployed today: ${deployed:,.2f} / ${self.config.MAX_DAILY_CAPITAL:,.2f}",
            f"  Positions open: {positions_open}",
            "",
        ]

        body = "\n".join(lines)

        msg            = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = self.config.SMTP_USER
        msg["To"]      = self.config.RECIPIENT_EMAIL

        self._send_email_async(
            msg,
            success_context=f"Trade alert [{action} {symbol} @ ${price:.2f}]",
            failure_context=f"Trade alert {action} {symbol} @ ${price:.2f}",
        )

    def send_daily_summary(self):
        """Send end-of-day summary email in a background thread. No-op if SMTP missing.

        Returns:
            None.
        """
        if not self.config.SMTP_USER or not self.config.SMTP_PASS:
            log.warning(
                "Daily email not sent  configure SMTP_USER and SMTP_PASS in .env "
                "(Gmail: use an App Password, not your account password)"
            )
            return

        try:
            data    = self._load_today_data()
            body    = self._build_body(data)
            net_pnl = data["summary"].get("net_pnl", 0)
            result  = "Profit" if net_pnl >= 0 else "Loss"
            subject = f"Trading Bot {data['today']} | {result} | P&L ${net_pnl:+.2f}"

            msg            = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"]    = self.config.SMTP_USER
            msg["To"]      = self.config.RECIPIENT_EMAIL

            self._send_email_async(
                msg,
                success_context=f"Daily summary (P&L ${net_pnl:+.2f})",
                failure_context="Daily summary email",
            )

        except Exception as e:
            log.error("Failed to prepare daily summary email: %s", e)
