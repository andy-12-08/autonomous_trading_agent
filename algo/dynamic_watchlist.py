"""
Dynamic watchlist  persists pre-Claude survivors across sessions.

Symbols that pass Stage 5 Pre-Claude Mechanical Vetoes are saved daily.
The next session's morning study uses this list for news headline fetching
so the AI gets catalyst context for yesterday's high-quality setups.

Falls back to config.WATCHLIST when no saved list exists.
"""
import json
import os
from datetime import date, datetime

import config
from core.database import log


class DynamicWatchlist:
    """Persists and loads pre-Claude survivor symbols across trading sessions.

    Symbols that pass mechanical veto checks are saved daily to a JSON file.
    On load, the saved list is merged with the fixed config.WATCHLIST and
    returned for use in the morning study's news headline fetch.

    Attributes:
        _WATCHLIST_PATH: Absolute path to the dynamic_watchlist.json file.
        _MAX_SYMBOLS: Maximum number of symbols to store (caps news quota usage).
    """

    def __init__(self):
        """Initialize DynamicWatchlist with file path and symbol cap.

        The watchlist JSON file is stored in the project root (one level above
        the ai/ subdirectory).
        """
        self._WATCHLIST_PATH = os.path.join(
            os.path.dirname(__file__), "..", "dynamic_watchlist.json"
        )
        self._MAX_SYMBOLS = 80

    def load(self) -> list[str]:
        """Load yesterday's pre-Claude survivors merged with the fixed watchlist.

        Returns config.WATCHLIST as fallback if no saved list exists or if the
        saved list is stale (older than 2 trading days).

        Returns:
            List of ticker symbols, deduplicated and capped at _MAX_SYMBOLS.
        """
        try:
            if not os.path.exists(self._WATCHLIST_PATH):
                log.info("Dynamic watchlist: no saved list found  using config.WATCHLIST")
                return list(config.WATCHLIST)

            with open(self._WATCHLIST_PATH, "r") as f:
                data = json.load(f)

            saved_date = data.get("date", "")
            symbols    = data.get("symbols", [])

            # Reject lists older than 2 calendar days (weekend gap = 3 days; be lenient)
            if saved_date:
                delta = (datetime.now(config.ET).date() - date.fromisoformat(saved_date)).days
                if delta > 3:
                    log.info(
                        "Dynamic watchlist: saved list from %s is stale (%d days)  using config.WATCHLIST",
                        saved_date, delta,
                    )
                    return list(config.WATCHLIST)

            if not symbols:
                return list(config.WATCHLIST)

            # Always include fixed watchlist; deduplicate
            merged = list(dict.fromkeys(symbols + list(config.WATCHLIST)))[: self._MAX_SYMBOLS]
            log.info(
                "Dynamic watchlist loaded: %d symbols (saved %s + %d fixed)",
                len(merged), saved_date, len(config.WATCHLIST),
            )
            return merged

        except Exception as e:
            log.warning("Dynamic watchlist load failed (%s)  falling back to config.WATCHLIST", e)
            return list(config.WATCHLIST)

    def save(self, survivors: list[str]) -> None:
        """Persist today's pre-Claude survivors for use in tomorrow's morning study.

        Merges with config.WATCHLIST to guarantee fixed symbols are always
        present in the next session's watchlist.

        Args:
            survivors: List of ticker symbols that passed all mechanical vetoes
                today. If empty, nothing is written to disk.
        """
        if not survivors:
            log.info("Dynamic watchlist: no survivors to save today")
            return

        # Deduplicate, preserving order; fixed watchlist appended after survivors
        merged = list(dict.fromkeys(survivors + list(config.WATCHLIST)))[: self._MAX_SYMBOLS]

        payload = {
            "date":    datetime.now(config.ET).date().isoformat(),
            "count":   len(merged),
            "symbols": merged,
        }
        try:
            with open(self._WATCHLIST_PATH, "w") as f:
                json.dump(payload, f, indent=2)
            log.info("Dynamic watchlist saved: %d symbols for %s", len(merged), payload["date"])
        except Exception as e:
            log.warning("Dynamic watchlist save failed (%s)", e)
