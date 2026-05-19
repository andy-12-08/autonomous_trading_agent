"""
Sector Bucket Manager  enforces diversification across the 6 sector buckets.

Rules:
  - Max 1 position per bucket at any time.
  - Exception: signal_confidence >= HIGH_CONVICTION_THRESHOLD (9/10) allows a
    second position in an occupied bucket (entry allowed; sizing unchanged).
  - All positions are risk-sized by risk_manager  bucket rules never override size.
  - Max 4 concurrent positions total; daily spend cap $4K enforced by risk_manager.
  - Rotation priority: fill empty buckets before revisiting occupied ones.

Sector rotation: leading sectors (outperforming SPY today) get priority in
  the watchlist ordering so the bot is exposed to the strongest money flows.
"""
import config


class BucketManager:
    """Enforces sector diversification across the 6 sector buckets.

    Prevents concentration in any single sector by limiting to one position
    per bucket (with high-conviction overrides). Also provides sector-strength
    ranking for prioritizing the watchlist by leading money flows.
    """

    # Sector ETF used to proxy each bucket's intraday relative strength vs SPY.
    SECTOR_ETF_MAP: dict[str, str] = {
        "tech":       "XLK",
        "consumer":   "XLY",
        "finance":    "XLF",
        "energy":     "XLE",
        "healthcare": "XLV",
        "industrial": "XLI",
        "index_etf":  "SPY",
    }

    @staticmethod
    def get_sector_strength(snapshots: dict[str, dict]) -> dict[str, float]:
        """Compute relative sector strength vs SPY from intraday ETF performance.

        Args:
            snapshots: {symbol: {price, change_pct, ...}} from broker.get_snapshots_bulk().

        Returns:
            {bucket: relative_strength} where positive = outperforming SPY today.
            Buckets without ETF data default to 0.0 (neutral).
        """
        spy_chg = (snapshots.get("SPY") or {}).get("change_pct", 0.0)
        result: dict[str, float] = {}
        for bucket, etf in BucketManager.SECTOR_ETF_MAP.items():
            data = snapshots.get(etf)
            if data:
                result[bucket] = round(float(data.get("change_pct", 0.0)) - spy_chg, 2)
            else:
                result[bucket] = 0.0
        return result

    @staticmethod
    def symbol_to_bucket(symbol: str) -> str:
        """Return the sector bucket for a given symbol, or 'unknown' if not mapped."""
        return config.SYMBOL_BUCKET.get(symbol.upper(), "unknown")

    @staticmethod
    def get_open_buckets(open_positions: list[dict]) -> dict[str, str]:
        """Return {bucket: symbol} for currently open positions."""
        return {
            BucketManager.symbol_to_bucket(p["symbol"]): p["symbol"]
            for p in open_positions
            if BucketManager.symbol_to_bucket(p["symbol"]) != "unknown"
        }

    @staticmethod
    def bucket_is_open(symbol: str, open_positions: list[dict],
                       signal_confidence: int = 0,
                       sector_strength: dict[str, float] | None = None) -> tuple[bool, str]:
        """Check whether a new position in this symbol's bucket is allowed.

        Screener-discovered stocks that are not in the sector classification get
        bucket="unknown". They are allowed to trade  the position-count cap in
        risk_manager is the binding constraint. We limit to 2 unclassified stocks
        open simultaneously to preserve some diversification discipline.

        Returns:
            Tuple of (allowed: bool, reason: str).
        """
        bucket    = BucketManager.symbol_to_bucket(symbol)
        open_bkts = BucketManager.get_open_buckets(open_positions)

        if bucket == "unknown":
            # Count how many unclassified stocks are already open
            unknown_open = sum(
                1 for p in open_positions
                if BucketManager.symbol_to_bucket(p["symbol"]) == "unknown"
            )
            if unknown_open >= 2:
                return False, (f"{symbol} is unclassified  already holding "
                               f"{unknown_open} unclassified stocks (max 2)")
            return True, f"{symbol} not in sector map  allowed (unclassified slot {unknown_open+1}/2)"

        if bucket not in open_bkts:
            return True, f"bucket '{bucket}' is empty  entry allowed"

        # Bucket already occupied
        incumbent = open_bkts[bucket]
        if signal_confidence >= config.HIGH_CONVICTION_THRESHOLD:
            return True, (f"High-conviction override ({signal_confidence}/10): "
                          f"allowing second position in '{bucket}' alongside {incumbent}")

        # Sector-hot override: confidence >= 8 + sector outperforming SPY >= 1.5% + incumbent profitable
        if signal_confidence >= 8 and sector_strength is not None:
            strength = sector_strength.get(bucket, 0.0)
            if strength >= 1.5:
                incumbent_pnl = next(
                    (p.get("pnl", 0.0) for p in open_positions if p["symbol"] == incumbent),
                    None,
                )
                if incumbent_pnl is not None and incumbent_pnl > 0:
                    return True, (
                        f"Sector-hot override ({signal_confidence}/10, '{bucket}' +{strength:.1f}% vs SPY, "
                        f"{incumbent} +${incumbent_pnl:.0f}): second position allowed"
                    )

        return False, (f"Bucket '{bucket}' already occupied by {incumbent}. "
                       f"Diversify to a different sector (rule: max 1/bucket).")

    @staticmethod
    def prioritize_watchlist(watchlist_data: list[dict],
                             open_positions: list[dict],
                             traded_buckets_today: set[str],
                             sector_strength: dict[str, float] | None = None) -> list[dict]:
        """Re-order the watchlist to surface the best sector opportunities first.

        Re-order watchlist_data so that:
          1. Empty buckets (never traded today) come first.
          2. Buckets already open are deprioritised.
          3. Among equal-priority buckets, leading sectors (positive relative strength
             vs SPY) are ranked before lagging sectors  institutional rotation logic.
          4. Within each group, order is preserved (caller already scored by indicators).
        """
        open_bkts = set(BucketManager.get_open_buckets(open_positions).keys())
        strength  = sector_strength or {}

        def sort_key(item):
            bkt = BucketManager.symbol_to_bucket(item["symbol"])
            already_open = bkt in open_bkts            # primary penalty: already holding
            traded_today = bkt in traded_buckets_today  # secondary penalty: rotated today
            # Tertiary: negative strength = leading sector ? lower sort key ? comes first
            rel_strength = -strength.get(bkt, 0.0)
            return (int(already_open), int(traded_today), rel_strength)

        return sorted(watchlist_data, key=sort_key)

    @staticmethod
    def build_bucket_report(open_positions: list[dict]) -> dict:
        """Return a summary of open exposure by bucket for context."""
        report = {b: None for b in config.SECTOR_BUCKETS}
        for pos in open_positions:
            bkt = BucketManager.symbol_to_bucket(pos["symbol"])
            if bkt in report:
                report[bkt] = {
                    "symbol": pos["symbol"],
                    "pnl_pct": pos.get("pnl_pct", 0),
                }
        return report
