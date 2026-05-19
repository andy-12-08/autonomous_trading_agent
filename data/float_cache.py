"""Float share data cache: fetches from yfinance, persists in SQLite with a 7-day TTL."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import config
from core.database import log


class FloatCache:
    """Fetch and cache float shares per symbol; 7-day TTL in SQLite."""

    CACHE_DAYS = 7

    def __init__(self, db_path: str | None = None):
        """Initialise with the SQLite path from config (or override for tests).

        Args:
            db_path: Override SQLite path; defaults to config.DB_PATH.
        """
        self._db_path = db_path or config.DB_PATH
        self._mem: dict[str, float | None] = {}  # in-process cache for the session
        self._lock = threading.Lock()
        self._init_table()

    def _init_table(self) -> None:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS float_cache (
                symbol     TEXT PRIMARY KEY,
                float_shares REAL,
                fetched_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    # -- Public API ------------------------------------------------------------

    def get_float_cached(self, symbol: str) -> float | None:
        """Return cached float shares without hitting the network.

        Returns the in-process value (fastest), then SQLite, then None when the
        symbol has never been fetched or the cache has expired.  Never blocks
        on a network call  use prefetch_floats() to fill the cache in bulk.

        Args:
            symbol: Uppercase ticker.

        Returns:
            Float shares as a float, or None when not in cache.
        """
        with self._lock:
            if symbol in self._mem:
                return self._mem[symbol]
        return self._read_db(symbol)

    def prefetch_floats(self, symbols: list[str], max_workers: int = 8) -> None:
        """Fetch and cache float data for all symbols not already in cache.

        Designed to run once per morning study so scanner lookups are instant.
        Network calls run in parallel; stale / missing entries are refreshed.

        Args:
            symbols: List of ticker strings to pre-warm.
            max_workers: Thread pool size for yfinance calls.

        Returns:
            None.
        """
        needed = [s for s in symbols if self._needs_refresh(s)]
        if not needed:
            log.info("FloatCache: all %d symbols already cached", len(symbols))
            return

        log.info("FloatCache: fetching float data for %d symbols", len(needed))
        fetched = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self._fetch_and_store, sym): sym for sym in needed}
            for fut in as_completed(futures):
                try:
                    fut.result()
                    fetched += 1
                except Exception as exc:
                    log.debug("FloatCache error for %s: %s", futures[fut], exc)
        log.info("FloatCache: refreshed %d/%d symbols", fetched, len(needed))

    @staticmethod
    def float_tier(float_shares: float | None) -> str:
        """Classify float size into a named tier.

        Args:
            float_shares: Number of publicly available shares, or None.

        Returns:
            "micro" (<5M), "small" (5M-20M), "mid" (20M-100M),
            "large" (>100M), or "unknown" when float_shares is None.
        """
        if float_shares is None:
            return "unknown"
        if float_shares < 5_000_000:
            return "micro"
        if float_shares < 20_000_000:
            return "small"
        if float_shares < 100_000_000:
            return "mid"
        return "large"

    # -- Internal helpers ------------------------------------------------------

    def _needs_refresh(self, symbol: str) -> bool:
        with self._lock:
            if symbol in self._mem:
                return False
        val = self._read_db(symbol)
        return val is None  # None means not in DB or expired

    def _read_db(self, symbol: str) -> float | None:
        try:
            conn = sqlite3.connect(self._db_path, timeout=5)
            row  = conn.execute(
                "SELECT float_shares, fetched_at FROM float_cache WHERE symbol=?",
                (symbol,),
            ).fetchone()
            conn.close()
            if row:
                age = datetime.now() - datetime.fromisoformat(row[1])
                if age < timedelta(days=self.CACHE_DAYS):
                    val = float(row[0]) if row[0] is not None else None
                    with self._lock:
                        self._mem[symbol] = val
                    return val
        except Exception:
            pass
        return None

    def _fetch_and_store(self, symbol: str) -> None:
        float_shares = self._fetch_yfinance(symbol)
        try:
            conn = sqlite3.connect(self._db_path, timeout=10)
            conn.execute(
                "INSERT OR REPLACE INTO float_cache (symbol, float_shares, fetched_at) VALUES (?,?,?)",
                (symbol, float_shares, datetime.now().isoformat()),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            log.debug("FloatCache DB write error for %s: %s", symbol, exc)
        with self._lock:
            self._mem[symbol] = float_shares

    @staticmethod
    def _fetch_yfinance(symbol: str) -> float | None:
        try:
            import yfinance as yf
            info = yf.Ticker(symbol).fast_info
            val  = getattr(info, "shares", None)
            return float(val) if val else None
        except Exception:
            return None
