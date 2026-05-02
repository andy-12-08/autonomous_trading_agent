import sqlite3
from datetime import date, timedelta
import config
from core.database import log


def _next_bday(d: date) -> date:
    """Next weekday after d.

    Returns:
        A date that is not Saturday/Sunday.
    """
    d += timedelta(days=1)
    while d.weekday() >= 5:   # 5=Sat 6=Sun
        d += timedelta(days=1)
    return d


class GFVTracker:
    def __init__(self, db_path: str):
        """Args:
            db_path: SQLite database path.
        """
        self.db_path = db_path

    def settlement_date_for_today(self) -> str:
        """Returns:
            ISO date string for the next business day (T+1 settlement anchor).
        """
        return _next_bday(date.today()).isoformat()

    def init_gfv_db(self) -> None:
        """Create gfv_positions if missing.

        Returns:
            None.
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gfv_positions (
                symbol             TEXT PRIMARY KEY,
                funded_by_settled  INTEGER NOT NULL DEFAULT 1,  -- 1=settled, 0=unsettled
                settlement_date    TEXT NOT NULL,               -- date proceeds settle
                entry_date         TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def record_buy(self, symbol: str, funded_by_settled: bool) -> None:
        """Tag a new buy as settled or GFV-locked.

        Args:
            symbol: Ticker that was purchased.
            funded_by_settled: True if the buy was funded by settled cash,
                False if funded by same-day unsettled proceeds.
        """
        settle = self.settlement_date_for_today()
        conn   = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT OR REPLACE INTO gfv_positions
               (symbol, funded_by_settled, settlement_date, entry_date)
               VALUES (?,?,?,?)""",
            (symbol, int(funded_by_settled), settle, date.today().isoformat()),
        )
        conn.commit()
        conn.close()
        if not funded_by_settled:
            log.warning("GFV-LOCK %s: bought with unsettled proceeds — locked until %s",
                        symbol, settle)

    def remove_buy(self, symbol: str) -> None:
        """Remove a symbol from GFV tracking (called after a position is fully closed)."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM gfv_positions WHERE symbol=?", (symbol,))
        conn.commit()
        conn.close()

    def is_gfv_locked(self, symbol: str) -> tuple[bool, str]:
        """Check whether selling this symbol would create a Good Faith Violation.

        Returns:
            Tuple of (locked: bool, reason: str). A position is locked if it was
            funded by unsettled proceeds AND those proceeds have not yet settled.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM gfv_positions WHERE symbol=?", (symbol,)
        ).fetchone()
        conn.close()

        if row is None:
            return False, "not tracked (assumed settled)"

        if row["funded_by_settled"]:
            return False, "funded by settled cash — no GFV risk"

        settle = date.fromisoformat(row["settlement_date"])
        if date.today() >= settle:
            return False, f"proceeds settled on {settle}"

        return True, (f"GFV-LOCK: funded by same-day unsettled proceeds; "
                      f"cannot sell until {settle}")

    def gfv_safe_to_sell(self, symbol: str) -> tuple[bool, str]:
        """Return (True, reason) if it is safe to sell this symbol without a GFV."""
        locked, reason = self.is_gfv_locked(symbol)
        return not locked, reason

    def get_available_settled_cash(self, alpaca_non_marginable_bp: float,
                                    deployed_today: float) -> float:
        """Compute true settled cash available for new buys.

        True settled cash = Alpaca's non_marginable_buying_power minus what
        we've already committed today. Capped to MAX_DAILY_CAPITAL headroom.

        Args:
            alpaca_non_marginable_bp: Alpaca's non_marginable_buying_power field.
            deployed_today: Dollar amount already committed in this session.

        Returns:
            Dollar amount of settled cash available for new positions.
        """
        daily_headroom = max(0.0, config.MAX_DAILY_CAPITAL - deployed_today)
        return min(alpaca_non_marginable_bp, daily_headroom)
