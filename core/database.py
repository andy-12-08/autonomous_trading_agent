import sqlite3
import logging
import json
from datetime import datetime
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
    def __init__(self, db_path: str):
        """Args:
            db_path: Path to the SQLite database file.
        """
        self.db_path = db_path

    def init_db(self) -> None:
        """Create tables (decisions, daily_summary, positions, daily_plans, …) and migrate columns.

        Returns:
            None.
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS decisions (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                TEXT NOT NULL,
                symbol            TEXT NOT NULL,
                action            TEXT NOT NULL,
                price             REAL,
                qty               REAL,
                stop_loss         REAL,
                take_profit       REAL,
                pnl               REAL,
                reasoning         TEXT,
                outcome           TEXT,
                outcome_pnl       REAL,
                setup_type        TEXT,
                confidence        INTEGER,
                signal_score      REAL,
                veto_rule         TEXT,
                slippage_dollars  REAL
            )
        """)
        # Migrate existing DB — add columns if they don't exist yet
        for col_sql in (
            "ALTER TABLE decisions ADD COLUMN setup_type TEXT",
            "ALTER TABLE decisions ADD COLUMN confidence INTEGER",
            "ALTER TABLE decisions ADD COLUMN signal_score REAL",
            "ALTER TABLE decisions ADD COLUMN veto_rule TEXT",
            "ALTER TABLE decisions ADD COLUMN slippage_dollars REAL",
        ):
            try:
                c.execute(col_sql)
            except Exception:
                pass  # column already exists
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
            CREATE TABLE IF NOT EXISTS positions (
                symbol        TEXT PRIMARY KEY,
                entry_price   REAL,
                qty           REAL,
                stop_loss     REAL,
                take_profit   REAL,
                entry_ts      TEXT,
                trailing      INTEGER DEFAULT 0,
                highest_price REAL,
                partial_taken INTEGER DEFAULT 0,
                setup_type    TEXT
            )
        """)
        # Migrate existing DB — add columns if absent
        for col_sql in (
            "ALTER TABLE positions ADD COLUMN partial_taken INTEGER DEFAULT 0",
            "ALTER TABLE positions ADD COLUMN setup_type TEXT",
        ):
            try:
                c.execute(col_sql)
            except Exception:
                pass
        c.execute("""
            CREATE TABLE IF NOT EXISTS gfv_positions (
                symbol             TEXT PRIMARY KEY,
                funded_by_settled  INTEGER NOT NULL DEFAULT 1,
                settlement_date    TEXT NOT NULL,
                entry_date         TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_plans (
                date TEXT PRIMARY KEY,
                plan TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def record_decision(self, symbol, action, price=None, qty=None, stop_loss=None,
                        take_profit=None, pnl=None, reasoning="", setup_type=None,
                        confidence=None, signal_score=None, veto_rule=None,
                        slippage_dollars=None) -> None:
        """Record a trading decision (BUY, SELL, SKIP, HOLD) to the database.

        Args:
            symbol: Ticker symbol (e.g. "AAPL").
            action: Decision type — one of BUY, SELL, SKIP, HOLD.
            price: Fill or reference price at decision time.
            qty: Share quantity involved in the decision.
            stop_loss: Stop-loss price level.
            take_profit: Take-profit price level.
            pnl: Realized P&L if this is a closing action.
            reasoning: Human-readable explanation of the decision.
            setup_type: Strategy label (e.g. "momentum", "reversal").
            confidence: Integer confidence score (0–100).
            signal_score: Composite signal score from the scorer.
            veto_rule: Name of the risk rule that blocked a trade, if any.
            slippage_dollars: Difference between expected and actual fill cost.

        Returns:
            None.
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO decisions
               (ts, symbol, action, price, qty, stop_loss, take_profit,
                pnl, reasoning, setup_type, confidence, signal_score, veto_rule,
                slippage_dollars)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (datetime.utcnow().isoformat(), symbol, action,
             price, qty, stop_loss, take_profit, pnl, reasoning, setup_type,
             confidence, signal_score, veto_rule, slippage_dollars),
        )
        conn.commit()
        conn.close()
        log.info("[%s] %s @ %.2f | qty=%.2f | SL=%.2f | TP=%.2f | pnl=%s | %s",
                 action, symbol, price or 0, qty or 0,
                 stop_loss or 0, take_profit or 0,
                 f"{pnl:.2f}" if pnl is not None else "n/a",
                 reasoning[:120])

    def update_outcome(self, symbol: str, outcome: str, outcome_pnl: float) -> None:
        """Link the most recent unlinked BUY for this symbol to its trade outcome.

        Args:
            symbol: Ticker symbol whose BUY record should be updated.
            outcome: Result label, e.g. "WIN" or "LOSS".
            outcome_pnl: Realized P&L for the completed trade.
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """UPDATE decisions SET outcome=?, outcome_pnl=?
               WHERE id = (
                   SELECT id FROM decisions
                   WHERE symbol=? AND action='BUY' AND outcome IS NULL
                   ORDER BY ts DESC LIMIT 1
               )""",
            (outcome, outcome_pnl, symbol),
        )
        conn.commit()
        conn.close()

    def save_position(self, symbol, entry_price, qty, stop_loss, take_profit,
                      trailing=False, highest_price=None, partial_taken=False,
                      entry_ts=None, setup_type=None) -> None:
        """Upsert a position record into the positions table.

        If setup_type is None and the symbol already exists in the table, the
        existing setup_type value is preserved.

        Args:
            symbol: Ticker symbol.
            entry_price: Price at which the position was entered.
            qty: Share quantity held.
            stop_loss: Current stop-loss price level.
            take_profit: Current take-profit price level.
            trailing: Whether trailing stop logic is active.
            highest_price: Highest price seen since entry (used for trailing stops).
            partial_taken: Whether a partial profit has already been taken.
            entry_ts: ISO timestamp of entry; defaults to utcnow if not provided.
            setup_type: Strategy label for the position.
        """
        conn = sqlite3.connect(self.db_path)
        # Preserve existing setup_type when not explicitly provided
        if setup_type is None:
            row = conn.execute(
                "SELECT setup_type FROM positions WHERE symbol=?", (symbol,)
            ).fetchone()
            if row:
                setup_type = row[0]
        conn.execute(
            """INSERT OR REPLACE INTO positions
               (symbol, entry_price, qty, stop_loss, take_profit, entry_ts,
                trailing, highest_price, partial_taken, setup_type)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (symbol, entry_price, qty, stop_loss, take_profit,
             entry_ts or datetime.utcnow().isoformat(),
             int(trailing), highest_price or entry_price, int(partial_taken), setup_type),
        )
        conn.commit()
        conn.close()

    def remove_position(self, symbol) -> None:
        """Delete a position record from the positions table.

        Args:
            symbol: Ticker symbol of the position to remove.
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
        conn.commit()
        conn.close()

    def get_open_positions_db(self) -> list[dict]:
        """Return all rows from the positions table as a list of dicts.

        Returns:
            List of position dicts, one per open position tracked in the DB.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM positions").fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_recent_decisions(self, limit=50) -> list[dict]:
        """Return the most recent trading decisions from the database.

        Args:
            limit: Maximum number of rows to return (default 50).

        Returns:
            List of decision dicts ordered newest-first.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM decisions ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def upsert_daily_summary(self, date_str, trades, wins, losses,
                             gross_pnl, net_pnl, notes="") -> None:
        """Insert or replace the daily trading summary for a given date.

        Args:
            date_str: ISO date string (YYYY-MM-DD) for the trading day.
            trades: Total number of completed trades.
            wins: Number of profitable trades.
            losses: Number of losing trades.
            gross_pnl: Total P&L before any fees/commissions.
            net_pnl: Total P&L after fees/commissions.
            notes: Optional free-text notes about the day.
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT OR REPLACE INTO daily_summary
               (date, trades, wins, losses, gross_pnl, net_pnl, notes)
               VALUES (?,?,?,?,?,?,?)""",
            (date_str, trades, wins, losses, gross_pnl, net_pnl, notes),
        )
        conn.commit()
        conn.close()
