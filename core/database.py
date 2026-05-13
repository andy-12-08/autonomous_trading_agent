import sqlite3
import logging
from datetime import datetime, timezone
import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("bot")

logging.getLogger("yfinance").setLevel(logging.CRITICAL)


class Database:
    """SQLite persistence for options positions, decisions, daily summaries, and plans."""

    def __init__(self, db_path: str):
        """Store the database path; each public method opens its own connection.

        Args:
            db_path: Filesystem path to the SQLite database file.
        """
        self.db_path = db_path

    def init_db(self) -> None:
        """Create options trading tables with WAL mode for concurrent access.

        Returns:
            None.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_summary (
                date      TEXT PRIMARY KEY,
                trades    INTEGER,
                wins      INTEGER,
                losses    INTEGER,
                gross_pnl REAL,
                net_pnl   REAL,
                notes     TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_plans (
                date TEXT PRIMARY KEY,
                plan TEXT NOT NULL
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS options_positions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id     TEXT NOT NULL UNIQUE,
                symbol          TEXT NOT NULL,
                strategy_type   TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'open',
                contracts       INTEGER NOT NULL DEFAULT 1,
                entry_ts        TEXT NOT NULL,
                close_ts        TEXT,
                expiry          TEXT,
                long_symbol     TEXT,
                short_symbol    TEXT,
                put_long_symbol  TEXT,
                put_short_symbol TEXT,
                call_short_symbol TEXT,
                call_long_symbol  TEXT,
                long_order_id   TEXT,
                short_order_id  TEXT,
                entry_premium   REAL,
                max_profit      REAL,
                max_loss        REAL,
                current_pnl     REAL DEFAULT 0,
                target_dte      INTEGER,
                short_delta     REAL,
                long_delta      REAL,
                entry_iv_rank   REAL,
                entry_vrp       REAL,
                net_delta       REAL,
                net_theta       REAL,
                net_vega        REAL,
                close_reason    TEXT,
                realized_pnl    REAL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS options_decisions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              TEXT NOT NULL,
                symbol          TEXT NOT NULL,
                strategy_type   TEXT,
                action          TEXT NOT NULL,
                position_id     TEXT,
                rationale       TEXT,
                iv_rank         REAL,
                iv_regime       TEXT,
                vrp             REAL,
                atm_iv          REAL,
                signal_score    REAL,
                market_regime   TEXT,
                veto_rule       TEXT,
                net_credit      REAL,
                net_debit       REAL,
                contracts       INTEGER,
                max_loss        REAL
            )
        """)
        conn.commit()
        conn.close()

    # ── Options CRUD ──────────────────────────────────────────────────────────

    def save_options_position(self, position_id: str, symbol: str,
                               strategy_type: str, contracts: int,
                               entry_premium: float, max_profit: float,
                               max_loss: float, expiry: str,
                               target_dte: int, entry_iv_rank: float,
                               entry_vrp: float, net_delta: float,
                               net_theta: float, net_vega: float,
                               short_delta: float = 0.0,
                               long_delta: float = 0.0,
                               long_symbol: str = None,
                               short_symbol: str = None,
                               put_long_symbol: str = None,
                               put_short_symbol: str = None,
                               call_short_symbol: str = None,
                               call_long_symbol: str = None,
                               long_order_id: str = None,
                               short_order_id: str = None) -> None:
        """Insert a new options position record.

        Args:
            position_id:     Unique ID string for this position (e.g. UUID).
            symbol:          Underlying ticker.
            strategy_type:   Strategy label (e.g. 'credit_put_spread').
            contracts:       Number of contracts.
            entry_premium:   Net premium received (credit) or paid (debit) per share.
            max_profit:      Maximum possible profit for the position in dollars.
            max_loss:        Maximum possible loss for the position in dollars.
            expiry:          Option expiry date string (YYYY-MM-DD).
            target_dte:      Target days to expiry at entry.
            entry_iv_rank:   IV Rank at time of entry.
            entry_vrp:       Volatility risk premium at time of entry.
            net_delta:       Portfolio delta of the position at entry.
            net_theta:       Portfolio theta of the position at entry.
            net_vega:        Portfolio vega of the position at entry.
            short_delta:     Target delta for the short leg.
            long_delta:      Target delta for the long leg.
            long_symbol:     OCC symbol for the long leg (spreads).
            short_symbol:    OCC symbol for the short leg (spreads).
            put_long_symbol: OCC symbol for the long put (iron condor).
            put_short_symbol: OCC symbol for the short put (iron condor).
            call_short_symbol: OCC symbol for the short call (iron condor).
            call_long_symbol:  OCC symbol for the long call (iron condor).
            long_order_id:   Alpaca order ID for the long leg.
            short_order_id:  Alpaca order ID for the short leg.

        Returns:
            None.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute(
            """INSERT INTO options_positions
               (position_id, symbol, strategy_type, status, contracts,
                entry_ts, expiry, long_symbol, short_symbol,
                put_long_symbol, put_short_symbol, call_short_symbol, call_long_symbol,
                long_order_id, short_order_id,
                entry_premium, max_profit, max_loss, target_dte,
                short_delta, long_delta,
                entry_iv_rank, entry_vrp, net_delta, net_theta, net_vega)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (position_id, symbol, strategy_type, "open", contracts,
             datetime.now(timezone.utc).isoformat(), expiry,
             long_symbol, short_symbol,
             put_long_symbol, put_short_symbol, call_short_symbol, call_long_symbol,
             long_order_id, short_order_id,
             entry_premium, max_profit, max_loss, target_dte,
             short_delta, long_delta,
             entry_iv_rank, entry_vrp, net_delta, net_theta, net_vega),
        )
        conn.commit()
        conn.close()
        log.info("Options position saved: %s %s x%d | premium=%.2f max_loss=%.2f",
                 strategy_type, symbol, contracts, entry_premium, max_loss)

    def update_options_position_pnl(self, position_id: str, current_pnl: float) -> None:
        """Update the live unrealized P&L for an open options position.

        Args:
            position_id: Unique position identifier.
            current_pnl: Current mark-to-market P&L in dollars.

        Returns:
            None.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute(
            "UPDATE options_positions SET current_pnl=? WHERE position_id=?",
            (current_pnl, position_id),
        )
        conn.commit()
        conn.close()

    def close_options_position(self, position_id: str, realized_pnl: float,
                                close_reason: str) -> None:
        """Mark an options position as closed with its final P&L and close reason.

        Args:
            position_id:  Unique position identifier.
            realized_pnl: Final realized P&L in dollars.
            close_reason: Short label explaining why the position was closed
                          (e.g. '50pct_profit', 'stop_loss', 'dte_exit').

        Returns:
            None.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute(
            """UPDATE options_positions
               SET status='closed', close_ts=?, realized_pnl=?, close_reason=?
               WHERE position_id=?""",
            (datetime.now(timezone.utc).isoformat(), realized_pnl, close_reason, position_id),
        )
        conn.commit()
        conn.close()
        log.info("Options position closed: %s | pnl=%.2f reason=%s",
                 position_id, realized_pnl, close_reason)

    def get_open_options_positions(self) -> list[dict]:
        """Return all currently open options positions.

        Returns:
            List of dicts, one per open options position row.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM options_positions WHERE status='open' ORDER BY entry_ts"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_options_position(self, position_id: str) -> dict | None:
        """Fetch a single options position by its unique ID.

        Args:
            position_id: The unique identifier for the position.

        Returns:
            Position dict, or None if not found.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM options_positions WHERE position_id=?", (position_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def record_options_decision(self, symbol: str, action: str,
                                 strategy_type: str = None,
                                 position_id: str = None,
                                 rationale: str = "",
                                 iv_rank: float = None,
                                 iv_regime: str = None,
                                 vrp: float = None,
                                 atm_iv: float = None,
                                 signal_score: float = None,
                                 market_regime: str = None,
                                 veto_rule: str = None,
                                 net_credit: float = None,
                                 net_debit: float = None,
                                 contracts: int = None,
                                 max_loss: float = None) -> None:
        """Log an options trading decision (ENTER, CLOSE, SKIP, ADJUST).

        Args:
            symbol:        Underlying ticker.
            action:        Decision type: ENTER, CLOSE, SKIP, or ADJUST.
            strategy_type: Strategy label for the decision.
            position_id:   Linked position ID (for CLOSE/ADJUST decisions).
            rationale:     Human-readable explanation of the decision.
            iv_rank:       IV Rank at decision time.
            iv_regime:     IV regime label ('high', 'neutral', 'low').
            vrp:           Volatility risk premium at decision time.
            atm_iv:        ATM implied volatility.
            signal_score:  Underlying directional signal score.
            market_regime: Current intraday market regime.
            veto_rule:     Risk rule that blocked a trade, if applicable.
            net_credit:    Net credit received for credit strategies.
            net_debit:     Net debit paid for debit strategies.
            contracts:     Number of contracts.
            max_loss:      Maximum possible loss for the position.

        Returns:
            None.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute(
            """INSERT INTO options_decisions
               (ts, symbol, strategy_type, action, position_id, rationale,
                iv_rank, iv_regime, vrp, atm_iv, signal_score, market_regime,
                veto_rule, net_credit, net_debit, contracts, max_loss)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (datetime.now(timezone.utc).isoformat(), symbol, strategy_type, action,
             position_id, rationale, iv_rank, iv_regime, vrp, atm_iv, signal_score,
             market_regime, veto_rule, net_credit, net_debit, contracts, max_loss),
        )
        conn.commit()
        conn.close()

    def get_today_options_decisions(self) -> list[dict]:
        """Return all options decisions logged today (UTC date).

        Returns:
            List of decision dicts for the current calendar day, newest-first.
        """
        today = datetime.now(timezone.utc).date().isoformat()
        conn  = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        rows  = conn.execute(
            "SELECT * FROM options_decisions WHERE ts LIKE ? ORDER BY ts DESC",
            (f"{today}%",),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def upsert_daily_summary(self, date_str, trades, wins, losses,
                             gross_pnl, net_pnl, notes="") -> None:
        """Insert or replace the daily trading summary for a given date.

        Args:
            date_str: ISO calendar date string for the trading day.
            trades: Total number of completed trades.
            wins: Number of profitable trades.
            losses: Number of losing trades.
            gross_pnl: Total P and L before fees or commissions.
            net_pnl: Total P and L after fees or commissions.
            notes: Optional free-text notes about the day.

        Returns:
            None.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute(
            """INSERT OR REPLACE INTO daily_summary
               (date, trades, wins, losses, gross_pnl, net_pnl, notes)
               VALUES (?,?,?,?,?,?,?)""",
            (date_str, trades, wins, losses, gross_pnl, net_pnl, notes),
        )
        conn.commit()
        conn.close()
