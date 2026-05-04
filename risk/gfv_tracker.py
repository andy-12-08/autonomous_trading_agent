import sqlite3
from datetime import date, timedelta, datetime
import config
from core.database import log


def _next_bday(d: date) -> date:
    """Return the next calendar date that is not a weekend.

    Args:
        d: Anchor calendar date.

    Returns:
        The first upcoming Monday-through-Friday date after d.
    """
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


class GFVTracker:
    """Track which buys used unsettled proceeds so sells can avoid GFV issues."""

    def __init__(self, db_path: str):
        """Store the SQLite path used for GFV position rows.

        Args:
            db_path: Same database file as the main trading journal.
        """
        self.db_path = db_path

    def settlement_date_for_today(self) -> str:
        """Return the next weekday after today as an ISO date anchor for T+1 logic.

        Returns:
            ISO-formatted calendar date string for the upcoming settlement anchor.
        """
        return _next_bday(datetime.now(config.ET).date()).isoformat()

    def init_gfv_db(self) -> None:
        """Ensure the gfv_positions table exists.

        Returns:
            None.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gfv_positions (
                symbol             TEXT PRIMARY KEY,
                funded_by_settled  INTEGER NOT NULL DEFAULT 1,
                settlement_date    TEXT NOT NULL,
                entry_date         TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def record_buy(self, symbol: str, funded_by_settled: bool) -> None:
        """Tag a new buy as settled or GFV-locked.

        Args:
            symbol: Ticker that was purchased.
            funded_by_settled: True when the buy used fully settled cash, False otherwise.

        Returns:
            None.
        """
        settle = self.settlement_date_for_today()
        conn   = sqlite3.connect(self.db_path, timeout=10)
        conn.execute(
            """INSERT INTO gfv_positions (symbol, funded_by_settled, settlement_date, entry_date)
               VALUES (?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET
                   funded_by_settled = MIN(funded_by_settled, excluded.funded_by_settled),
                   settlement_date   = CASE
                       WHEN excluded.funded_by_settled = 0
                            AND excluded.settlement_date > settlement_date
                           THEN excluded.settlement_date
                       ELSE settlement_date
                   END""",
            (symbol, int(funded_by_settled), settle, datetime.now(config.ET).date().isoformat()),
        )
        conn.commit()
        conn.close()
        if not funded_by_settled:
            log.warning("GFV-LOCK %s: bought with unsettled proceeds — locked until %s",
                        symbol, settle)

    def remove_buy(self, symbol: str) -> None:
        """Delete GFV metadata after a symbol is fully closed.

        Args:
            symbol: Ticker to remove from the tracking table.

        Returns:
            None.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("DELETE FROM gfv_positions WHERE symbol=?", (symbol,))
        conn.commit()
        conn.close()

    def is_gfv_locked(self, symbol: str) -> tuple[bool, str]:
        """Check whether selling would violate good-faith rules for unsettled funding.

        Args:
            symbol: Ticker to inspect in SQLite.

        Returns:
            Tuple of locked boolean and a human-readable explanation string.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
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
        if datetime.now(config.ET).date() >= settle:
            return False, f"proceeds settled on {settle}"

        return True, (f"GFV-LOCK: funded by same-day unsettled proceeds; "
                      f"cannot sell until {settle}")

    def gfv_safe_to_sell(self, symbol: str) -> tuple[bool, str]:
        """Return whether a sell is GFV-safe plus the underlying explanation string.

        Args:
            symbol: Ticker to evaluate.

        Returns:
            Tuple where the first value is True when selling is allowed, False when locked.
        """
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
