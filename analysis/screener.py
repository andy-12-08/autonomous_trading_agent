"""
Dynamic stock universe screener — two-stage funnel.

Stage 1 — Snapshot screen (runs every 15 min, ~10 API calls):
  Fetches price + volume + % change for ALL active NYSE/NASDAQ stocks via
  Alpaca's bulk snapshot endpoint. Applies quality filters and returns the
  top 300 by dollar-volume × momentum. This gives near-complete market
  coverage without fetching full bar data for every symbol.

Stage 2 — Signal analysis (existing pipeline in trader.py):
  build_watchlist_data() runs full bar + indicator analysis only on the
  symbols that passed Stage 1. The signal scorer then drops anything below
  the quality threshold before Claude ever sees it.

Fallback: if the snapshot screen fails, the most-actives + gainers screener
  and fixed watchlist ensure trading is never blocked.
"""
import requests
from datetime import datetime
import config

from core.database import log

_DATA_BASE = "https://data.alpaca.markets"


class Screener:
    """
    Two-stage stock universe screener that combines broad snapshot sweeps
    with focused most-actives and gainers feeds.

    Args:
        broker: Broker client instance with get_all_tradeable_symbols() and
                get_snapshots_bulk() methods.
    """

    def __init__(self, broker):
        """
        Initialize the Screener with a broker client.

        Args:
            broker: Broker client providing market data API access.
        """
        self.broker = broker
        self._snapshot_cache: list[str] = []
        self._snapshot_cache_ts = None
        self._snapshot_raw: list[dict] = []   # raw candidates with change_pct, used by gainers
        self.SNAPSHOT_CACHE_TTL_MIN = 30
        self.SNAPSHOT_MIN_DOLLAR_VOLUME = 5_000_000   # $5M — institutional liquidity threshold
        self.SNAPSHOT_MIN_MOVE_PCT = 0.5              # must be moving ≥ 0.5% from yesterday

    @staticmethod
    def _headers() -> dict:
        """
        Build Alpaca API authentication headers.

        Returns:
            Dict with APCA key/secret headers and accept type.
        """
        return {
            "APCA-API-KEY-ID":     config.ALPACA_KEY or "",
            "APCA-API-SECRET-KEY": config.ALPACA_SECRET or "",
            "accept": "application/json",
        }

    @staticmethod
    def _valid_sym(sym: str) -> bool:
        """
        Validate that a symbol string is a tradeable US equity ticker.

        Args:
            sym: Raw symbol string to validate.

        Returns:
            True if the symbol is non-empty, alphabetic, ≤5 chars, and not
            a crypto pair (no "/" separator).
        """
        sym = sym.strip().upper()
        return bool(sym) and "/" not in sym and len(sym) <= 5 and sym.isalpha()

    def _fetch_most_actives(self, top: int = 100) -> list[str]:
        """
        Fetch top stocks by share volume — confirms real institutional interest.

        Args:
            top: Maximum number of symbols to return.

        Returns:
            List of valid symbol strings sorted by volume, or empty list on failure.
        """
        try:
            resp = requests.get(
                f"{_DATA_BASE}/v1beta1/screener/stocks/most-actives",
                headers=Screener._headers(),
                params={"by": "volume", "top": top},
                timeout=8,
            )
            resp.raise_for_status()
            symbols = [
                item["symbol"]
                for item in resp.json().get("most_actives", [])
                if Screener._valid_sym(item.get("symbol", ""))
            ]
            log.info("Screener most-actives: %d symbols", len(symbols))
            return symbols
        except Exception as e:
            log.warning("most-actives screener failed (%s) — falling back", e)
            return []

    def _fetch_gainers(self, top: int = 50) -> list[str]:
        """
        Return top gainers by % change from the snapshot, with a live fallback.

        Primary: derives from _snapshot_raw (no extra API call needed). The snapshot
        uses today's open as the reference when IEX omits prev_daily_bar, so this
        correctly captures intraday movers.

        Fallback: when the snapshot yields zero results (e.g. pre-market or IEX data
        gap), calls most-actives by=trades as a live proxy for high-activity movers.

        Args:
            top: Maximum number of gainers to return.

        Returns:
            List of symbol strings sorted by % change descending (or by trade count
            for the fallback), capped at top.
        """
        if self._snapshot_raw:
            gainers = sorted(
                (d for d in self._snapshot_raw if d["change_pct"] >= 0.5),
                key=lambda d: d["change_pct"],
                reverse=True,
            )
            symbols = [d["sym"] for d in gainers[:top]]
            log.info("Screener top-gainers: %d symbols (derived from snapshot)", len(symbols))
            if symbols:
                return symbols

        # Fallback: snapshot yielded nothing — use most-actives by trade count as a
        # proxy for stocks in motion (captures gap-ups and catalyst plays live).
        log.info("Screener gainers: snapshot empty — falling back to most-actives by trades")
        try:
            resp = requests.get(
                f"{_DATA_BASE}/v1beta1/screener/stocks/most-actives",
                headers=Screener._headers(),
                params={"by": "trades", "top": top},
                timeout=8,
            )
            resp.raise_for_status()
            symbols = [
                item["symbol"]
                for item in resp.json().get("most_actives", [])
                if Screener._valid_sym(item.get("symbol", ""))
            ]
            log.info("Screener top-gainers fallback (by trades): %d symbols", len(symbols))
            return symbols
        except Exception as e:
            log.warning("Screener gainers fallback failed (%s)", e)
            return []

    def _snapshot_screen(self, top: int = 300) -> list[str]:
        """
        Stage 1: broad market sweep using Alpaca's bulk snapshot API.

        Gets current price + today's volume + % change for every active NYSE/NASDAQ
        stock in ~10 API calls. Filters by price band, dollar volume, and minimum
        price movement. Returns the top N ranked by dollar_volume × |change_pct|.

        Result is cached for SNAPSHOT_CACHE_TTL_MIN minutes — fast cycles reuse it.

        Args:
            top: Maximum number of symbols to return after ranking.

        Returns:
            Ranked list of symbol strings, or cached list if still fresh.
        """
        now = datetime.now()
        if (self._snapshot_cache and self._snapshot_cache_ts and
                (now - self._snapshot_cache_ts).total_seconds() < self.SNAPSHOT_CACHE_TTL_MIN * 60):
            log.info("Snapshot screen: cache hit (%d symbols, age=%.0fs)",
                     len(self._snapshot_cache),
                     (now - self._snapshot_cache_ts).total_seconds())
            return self._snapshot_cache

        all_symbols = self.broker.get_all_tradeable_symbols()
        if not all_symbols:
            log.warning("Snapshot screen: asset list empty — skipping")
            return []

        log.info("Snapshot screen: fetching snapshots for %d symbols in batches…",
                 len(all_symbols))
        t0        = datetime.now()
        snapshots = self.broker.get_snapshots_bulk(all_symbols)
        elapsed   = (datetime.now() - t0).total_seconds()
        log.info("Snapshot screen: %d/%d symbols returned data in %.1fs",
                 len(snapshots), len(all_symbols), elapsed)

        # Stage 1: apply price and liquidity filters, collect raw candidates
        raw_candidates: list[dict] = []
        for sym, data in snapshots.items():
            price      = data["price"]
            dollar_vol = data["dollar_volume"]
            change_pct = data["change_pct"]

            if not (config.SCREENER_MIN_PRICE <= price <= config.SCREENER_MAX_PRICE):
                continue
            if dollar_vol <= 0:
                continue

            raw_candidates.append({"sym": sym, "dollar_vol": dollar_vol,
                                    "change_pct": change_pct})

        # Stage 2: cross-sectional momentum rank (institutional approach).
        # Rank every stock by today's % change vs the rest of the universe.
        # Top-quartile movers get a 50% scoring boost on top of dollar volume.
        if len(raw_candidates) > 1:
            sorted_by_chg = sorted(raw_candidates, key=lambda x: x["change_pct"])
            n = len(sorted_by_chg)
            rank_lookup = {d["sym"]: (i + 1) / n for i, d in enumerate(sorted_by_chg)}
        else:
            rank_lookup = {}

        candidates: list[tuple[str, float]] = []
        for d in raw_candidates:
            sym          = d["sym"]
            dollar_vol   = d["dollar_vol"]
            change_pct   = d["change_pct"]
            mom_rank     = rank_lookup.get(sym, 0.5)      # 0=weakest, 1=strongest
            # Score = dollar_vol × (1 + |change|/100) × (1 + cross-sectional rank × 0.5)
            # Stocks in top momentum percentile get up to 50% boost over same-dollar-vol peers.
            score = dollar_vol * (1 + abs(change_pct) / 100) * (1 + mom_rank * 0.5)
            candidates.append((sym, score))

        candidates.sort(key=lambda x: x[1], reverse=True)
        result = [sym for sym, _ in candidates[:top]]

        log.info("Snapshot screen: %d/%d passed filters → returning top %d",
                 len(candidates), len(snapshots), len(result))

        self._snapshot_cache    = result
        self._snapshot_cache_ts = now
        self._snapshot_raw      = raw_candidates   # preserve for gainers derivation
        return result

    def build_universe(self) -> list[str]:
        """
        Return a deduplicated, priority-ordered list of symbols to scan this cycle.

        Two key design principles:
          1. Fixed watchlist stocks are EXCLUDED from screener results — they are
             guaranteed every cycle anyway, so screener slots must go to discovery.
          2. Each source gets a protected quota so gainers always contributes fresh
             catalyst plays even when snapshot and most-actives overlap heavily.

        Slot allocation (config.py):
          - Snapshot  : SCREENER_SNAPSHOT_SLOTS  (50) non-watchlist stocks
          - Actives   : SCREENER_ACTIVES_SLOTS   (30) non-watchlist, not in snapshot
          - Gainers   : SCREENER_GAINERS_SLOTS   (20) non-watchlist, not in above
          - Watchlist : always appended last — guaranteed regardless of screener

        Total capped at UNIVERSE_MAX_SYMBOLS (150).

        Returns:
            Final symbol list ordered by priority (snapshot → actives → gainers →
            fixed watchlist), capped at config.UNIVERSE_MAX_SYMBOLS.
        """
        watchlist_set = set(config.WATCHLIST)
        seen:   set[str]  = set()
        result: list[str] = []

        def add(sym: str) -> bool:
            sym = sym.strip().upper()
            if sym and sym not in seen:
                seen.add(sym)
                result.append(sym)
                return True
            return False

        def add_from(source: list[str], slots: int, label: str) -> int:
            """Add up to slots symbols from source, skipping watchlist and already-seen."""
            added = 0
            for sym in source:
                if added >= slots:
                    break
                sym = sym.strip().upper()
                if sym in watchlist_set:
                    continue          # watchlist already guaranteed — don't waste a slot
                if add(sym):
                    added += 1
            log.info("Screener %s: %d new discovery slots filled", label, added)
            return added

        # 1. Snapshot — broadest sweep, refreshed every 15 min
        #    Fetch more than the slot count so filtering watchlist stocks doesn't
        #    leave us short (e.g. request 150, keep first 50 non-watchlist)
        snap_raw = self._snapshot_screen(top=150)
        add_from(snap_raw, config.SCREENER_SNAPSHOT_SLOTS, "snapshot")

        # 2. Most-actives — real-time volume leaders (refreshed every cycle)
        actives_raw = self._fetch_most_actives(top=100)
        add_from(actives_raw, config.SCREENER_ACTIVES_SLOTS, "actives")

        # 3. Gainers — catalyst / % movers (refreshed every cycle)
        #    This is where SNDK-type earnings-catalyst plays surface
        gainers_raw = self._fetch_gainers(top=80)
        add_from(gainers_raw, config.SCREENER_GAINERS_SLOTS, "gainers")

        # 4. Fixed watchlist — always appended last, guaranteed every cycle
        for sym in config.WATCHLIST:
            add(sym)

        final       = result[:config.UNIVERSE_MAX_SYMBOLS]
        disc_count  = sum(1 for s in final if s not in watchlist_set)
        fixed_count = sum(1 for s in final if s in watchlist_set)
        log.info("Universe: %d symbols total (%d discovery + %d fixed watchlist)",
                 len(final), disc_count, fixed_count)
        return final
