"""
Email notifier: formats and sends trade alerts and daily summaries for the options bot.

Two alert types:
  send_options_entry_alert — fired when a new options position is opened
  send_options_close_alert — fired when a position is closed (win, loss, or DTE exit)

Both are sent from daemon threads so they never block the trading cycle.
All methods are no-ops when SMTP credentials are not configured.
"""

import smtplib
import sqlite3
import json
import threading
from email.mime.text import MIMEText
from datetime import datetime
from typing import Optional

import config
from core.database import log


class Notifier:
    """Format and send SMTP alerts for the options trading bot in background threads."""

    def __init__(self, config_module, db_path: str):
        """
        Store config reference and database path.

        Args:
            config_module: Module providing SMTP_HOST, SMTP_USER, SMTP_PASS,
                           RECIPIENT_EMAIL, LOG_FILE, etc.
            db_path:       SQLite database path for daily summary queries.
        """
        self.config  = config_module
        self.db_path = db_path

    # ── Options entry alert ───────────────────────────────────────────────────

    def send_options_entry_alert(
        self,
        strategy_type: str,
        symbol:        str,
        contracts:     int,
        entry_premium: float,
        max_profit:    float,
        max_loss:      float,
        expiry:        str,
        dte:           int,
        iv_rank:       float,
        vrp:           float,
        rationale:     str,
        long_symbol:   Optional[str] = None,
        short_symbol:  Optional[str] = None,
    ) -> None:
        """
        Send an email alert when a new options position is entered.

        Args:
            strategy_type: e.g. 'credit_put_spread', 'iron_condor'.
            symbol:        Underlying ticker.
            contracts:     Number of contracts.
            entry_premium: Net premium received (credit) or paid (debit) per share.
            max_profit:    Maximum possible profit in dollars.
            max_loss:      Maximum possible loss in dollars.
            expiry:        Option expiry date string (YYYY-MM-DD).
            dte:           Days to expiry at entry.
            iv_rank:       IV Rank at entry time.
            vrp:           Volatility risk premium in volatility points.
            rationale:     Strategy selection reason.
            long_symbol:   OCC symbol for the long leg (spreads).
            short_symbol:  OCC symbol for the short leg (spreads).

        Returns:
            None.
        """
        if not self.config.SMTP_USER or not self.config.SMTP_PASS:
            return

        now_et     = datetime.now(config.ET).strftime("%Y-%m-%d %H:%M:%S ET")
        is_credit  = strategy_type in ("credit_put_spread", "credit_call_spread", "iron_condor")
        prem_label = "Credit received" if is_credit else "Debit paid"
        direction  = "📉 SELL PREMIUM" if is_credit else "📈 BUY SPREAD"
        rr         = round(max_profit / max_loss, 2) if max_loss > 0 else 0

        subject = (
            f"{direction} | {symbol} {strategy_type.upper().replace('_', ' ')} "
            f"x{contracts} | R:R={rr:.1f}"
        )

        sep = "─" * 58

        lines = [
            "╔══════════════════════════════════════════════════════════╗",
            f"  OPTIONS ENTRY ALERT — {strategy_type.upper().replace('_', ' ')}",
            f"  {symbol}  |  {now_et}",
            "╚══════════════════════════════════════════════════════════╝",
            "",
            sep,
            "  TRADE DETAILS",
            sep,
            f"  Underlying  : {symbol}",
            f"  Strategy    : {strategy_type.replace('_', ' ')}",
            f"  Contracts   : {contracts}",
            f"  Expiry      : {expiry}  ({dte} DTE)",
            f"  {prem_label:<14}: ${entry_premium:.2f}/share  (${entry_premium*100*contracts:.2f} total)",
            f"  Max profit  : ${max_profit:.2f}",
            f"  Max loss    : ${max_loss:.2f}",
            f"  R:R         : {rr:.2f}",
        ]

        if long_symbol:
            lines.append(f"  Long leg    : {long_symbol}")
        if short_symbol:
            lines.append(f"  Short leg   : {short_symbol}")

        lines += [
            "",
            sep,
            "  IV CONTEXT",
            sep,
            f"  IV Rank     : {iv_rank:.0f}%",
            f"  VRP         : {vrp:.1f} pts",
            f"  Rationale   : {rationale[:100]}",
            "",
            sep,
            "  EXIT RULES (mechanical)",
            sep,
            f"  Take profit : when spread is at 50% of entry premium",
            f"  Stop loss   : {200 if is_credit else 50}% of credit received (credit) / 50% of debit paid",
            f"  DTE exit    : ≤ {config.CREDIT_CLOSE_DTE_DAYS if is_credit else config.DEBIT_CLOSE_DTE_DAYS} DTE",
            "",
        ]

        self._send_async(subject, "\n".join(lines))

    # ── Options close alert ───────────────────────────────────────────────────

    def send_options_close_alert(
        self,
        strategy_type: str,
        symbol:        str,
        contracts:     int,
        realized_pnl:  float,
        pnl_pct:       float,
        close_reason:  str,
        entry_premium: float,
        max_profit:    float,
        equity:        float,
        daily_pnl:     float,
        rationale:     str,
    ) -> None:
        """
        Send an email alert when an options position is closed.

        Args:
            strategy_type: Strategy label string.
            symbol:        Underlying ticker.
            contracts:     Number of contracts closed.
            realized_pnl:  Actual realized P&L in dollars.
            pnl_pct:       P&L as percent of max loss (negative = loss).
            close_reason:  Short reason label (e.g. '50pct_profit', 'stop_loss').
            entry_premium: Entry premium per share.
            max_profit:    Maximum possible profit for the position.
            equity:        Account equity at close time.
            daily_pnl:     Session realized P&L after this close.
            rationale:     Human-readable close reason.

        Returns:
            None.
        """
        if not self.config.SMTP_USER or not self.config.SMTP_PASS:
            return

        now_et = datetime.now(config.ET).strftime("%Y-%m-%d %H:%M:%S ET")

        if realized_pnl >= 0:
            emoji   = "✅"
            outcome = "WIN"
        else:
            emoji   = "🔴"
            outcome = "LOSS"

        capture_pct = (realized_pnl / max_profit * 100) if max_profit > 0 else 0

        subject = (
            f"{emoji} OPTIONS CLOSE | {symbol} {outcome} "
            f"${realized_pnl:+.2f} | {close_reason.replace('_', ' ').upper()}"
        )

        sep = "─" * 58

        lines = [
            "╔══════════════════════════════════════════════════════════╗",
            f"  OPTIONS CLOSE ALERT — {outcome}",
            f"  {symbol}  |  {now_et}",
            "╚══════════════════════════════════════════════════════════╝",
            "",
            sep,
            "  CLOSE DETAILS",
            sep,
            f"  Symbol      : {symbol}",
            f"  Strategy    : {strategy_type.replace('_', ' ')}",
            f"  Contracts   : {contracts}",
            f"  Close reason: {close_reason.replace('_', ' ')}",
            f"  Realized P&L: ${realized_pnl:+.2f}",
            f"  Max profit  : ${max_profit:.2f}",
            f"  Captured    : {capture_pct:.0f}% of max profit",
            f"  Rationale   : {rationale[:100]}",
            "",
            sep,
            "  ACCOUNT SNAPSHOT",
            sep,
            f"  Total equity : ${equity:,.2f}",
            f"  Today P&L    : ${daily_pnl:+.2f}",
            "",
        ]

        self._send_async(subject, "\n".join(lines))

    # ── Daily summary ─────────────────────────────────────────────────────────

    def send_daily_summary(self) -> None:
        """
        Send end-of-day summary email in a background thread.

        No-op if SMTP credentials are not configured.
        """
        if not self.config.SMTP_USER or not self.config.SMTP_PASS:
            log.warning(
                "Daily email not sent — configure SMTP_USER and SMTP_PASS in .env "
                "(Gmail: use an App Password)"
            )
            return

        try:
            data    = self._load_today_data()
            body    = self._build_options_body(data)
            net_pnl = data["summary"].get("net_pnl", 0)
            emoji   = "✅" if net_pnl >= 0 else "🔴"
            subject = (
                f"{emoji} Options Bot {data['today']} | P&L ${net_pnl:+.2f} | "
                f"{data['enters']} enters {data['closes']} closes"
            )

            self._send_async(subject, body)

        except Exception as exc:
            log.error("Failed to prepare daily summary email: %s", exc)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _load_today_data(self) -> dict:
        """
        Load today's options decisions, positions, and daily summary from the DB.

        Returns:
            Dict with keys: today, decisions, closed_positions, summary, plan,
                            enters, closes, skips.
        """
        today = datetime.now(config.ET).date().isoformat()
        conn  = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row

        decisions = [dict(r) for r in conn.execute(
            "SELECT * FROM options_decisions WHERE ts LIKE ? ORDER BY ts",
            (f"{today}%",),
        ).fetchall()]

        closed_positions = [dict(r) for r in conn.execute(
            "SELECT * FROM options_positions WHERE status='closed' AND close_ts LIKE ? ORDER BY close_ts",
            (f"{today}%",),
        ).fetchall()]

        summary_row = conn.execute(
            "SELECT * FROM daily_summary WHERE date=?", (today,)
        ).fetchone()
        summary = dict(summary_row) if summary_row else {}

        plan_row = conn.execute(
            "SELECT plan FROM daily_plans WHERE date=?", (today,)
        ).fetchone()
        plan = json.loads(plan_row["plan"]) if plan_row else {}

        conn.close()

        enters = sum(1 for d in decisions if d.get("action") == "ENTER")
        closes = sum(1 for d in decisions if d.get("action") == "CLOSE")
        skips  = sum(1 for d in decisions if d.get("action") == "SKIP")

        return {
            "today":            today,
            "decisions":        decisions,
            "closed_positions": closed_positions,
            "summary":          summary,
            "plan":             plan,
            "enters":           enters,
            "closes":           closes,
            "skips":            skips,
        }

    def _build_options_body(self, data: dict) -> str:
        """
        Format the plaintext body for the options daily summary email.

        Args:
            data: Output of _load_today_data.

        Returns:
            Formatted email body string.
        """
        s        = data["summary"]
        p        = data["plan"]
        closed   = data["closed_positions"]
        enters   = data["enters"]
        closes   = data["closes"]
        today    = data["today"]

        net_pnl   = s.get("net_pnl",  0)
        wins      = sum(1 for pos in closed if (pos.get("realized_pnl") or 0) > 0)
        losses    = sum(1 for pos in closed if (pos.get("realized_pnl") or 0) < 0)
        win_rate  = wins / closes if closes else 0

        avg_credit_captured = 0.0
        credit_positions    = [pos for pos in closed
                                if pos.get("strategy_type", "") in
                                ("credit_put_spread", "credit_call_spread", "iron_condor")]
        if credit_positions:
            captures = []
            for pos in credit_positions:
                mp = float(pos.get("max_profit") or 0)
                rp = float(pos.get("realized_pnl") or 0)
                if mp > 0:
                    captures.append(rp / mp * 100)
            if captures:
                avg_credit_captured = sum(captures) / len(captures)

        status_line = "PROFITABLE DAY" if net_pnl >= 0 else "LOSS DAY"
        sep = "─" * 58

        lines = [
            "╔══════════════════════════════════════════════════════════╗",
            "  OPTIONS BOT — DAILY REPORT",
            f"  {today}",
            "╚══════════════════════════════════════════════════════════╝",
            f"  {status_line}",
            "",
            sep,
            "  TODAY'S PERFORMANCE",
            sep,
            f"  Net P&L          : ${net_pnl:+.2f}",
            f"  Entries          : {enters}",
            f"  Closes           : {closes}",
            f"  Wins / Losses    : {wins} / {losses}",
            f"  Win rate         : {win_rate:.0%}",
        ]

        if avg_credit_captured:
            lines.append(f"  Avg credit capt. : {avg_credit_captured:.0f}%")

        if p:
            lines += [
                "",
                sep,
                "  TODAY'S MARKET CONTEXT",
                sep,
                f"  Bias      : {p.get('market_bias', 'N/A')}",
                f"  Posture   : {p.get('risk_posture', 'N/A')}",
                f"  Summary   : {p.get('market_summary', 'N/A')[:120]}",
            ]

        if closed:
            lines += ["", sep, "  CLOSED POSITIONS", sep]
            for pos in closed:
                pnl    = float(pos.get("realized_pnl") or 0)
                mp     = float(pos.get("max_profit")   or 0)
                capt   = f"{pnl/mp*100:.0f}%" if mp > 0 else "—"
                reason = pos.get("close_reason", "—")
                strat  = pos.get("strategy_type", "").replace("_", " ")
                lines.append(
                    f"  {pos['symbol']:6s}  {strat:22s}  "
                    f"pnl={pnl:+8.2f}  captured={capt:>6}  [{reason}]"
                )

        lines += [
            "",
            sep,
            f"  Log: {self.config.LOG_FILE}  |  DB: {self.db_path}",
            "",
        ]

        return "\n".join(lines)

    def _send_async(self, subject: str, body: str) -> None:
        """Send an email in a background daemon thread.

        Args:
            subject: Email subject line.
            body:    Plaintext email body.
        """
        cfg = self.config

        msg            = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = cfg.SMTP_USER
        msg["To"]      = cfg.RECIPIENT_EMAIL

        def _send():
            try:
                with smtplib.SMTP(cfg.SMTP_HOST, cfg.SMTP_PORT, timeout=30) as server:
                    server.ehlo()
                    server.starttls()
                    server.login(cfg.SMTP_USER, cfg.SMTP_PASS)
                    server.send_message(msg)
                log.info("Email sent → %s  [%s]", cfg.RECIPIENT_EMAIL, subject[:60])
            except Exception as exc:
                log.error("Email failed: %s — subject: %s", exc, subject[:60])

        threading.Thread(target=_send, daemon=True).start()
