import config
from core.database import log


class RiskManager:
    """Static helpers to approve buys, size positions, and compute protective levels."""
    @staticmethod
    def approve_buy(symbol: str, price: float, qty: float, stop_loss: float,
                    settled_cash: float, deployed_today: float, num_positions: int,
                    daily_pnl: float, total_equity: float, trades_today: int,
                    reward_to_risk: float, signal_confidence: int,
                    vol_ratio: float, rsi: float,
                    spread_pct: float | None = None,
                    key_levels: dict | None = None,
                    min_vol_ratio_override: float | None = None) -> tuple[bool, str]:
        """Validate a BUY against risk rules (drawdown, exposure, cash, R:R, etc.).

        Args:
            symbol: Ticker.
            price: Entry price.
            qty: Share count.
            stop_loss: Stop price.
            settled_cash: Available settled cash.
            deployed_today: Capital already deployed today.
            num_positions: Current open position count.
            daily_pnl: Today's P&L (effective).
            total_equity: Account equity.
            trades_today: Completed BUY count today.
            reward_to_risk: Planned R:R.
            signal_confidence: AI confidence 1–10.
            vol_ratio: Volume vs average.
            rsi: Latest RSI.
            spread_pct: Bid/ask spread fraction, optional.
            key_levels: Structural levels for proximity checks, optional.
            min_vol_ratio_override: Optional floor for vol_ratio (early window).

        Returns:
            (True, "OK") if approved; (False, reason) if vetoed.
        """
        cost = price * qty

        if daily_pnl <= -config.DAILY_DRAWDOWN_LIMIT:
            return False, f"Daily drawdown limit hit (${daily_pnl:.0f} ≤ -${config.DAILY_DRAWDOWN_LIMIT:.0f})"

        # Rule 6: early warning at 1.5%
        warning_level = total_equity * 0.015
        if daily_pnl <= -warning_level:
            log.warning("⚠ Drawdown warning: daily P&L at $%.0f (%.1f%% of equity) — "
                        "tighten stops, no new aggressive entries",
                        daily_pnl, abs(daily_pnl / total_equity * 100))

        # Rule 16: max trades per day
        if trades_today >= config.MAX_TRADES_PER_DAY:
            return False, f"Max trades/day ({config.MAX_TRADES_PER_DAY}) reached"

        # Rule 7: total exposure cap (≤ MAX_TOTAL_EXPOSURE_PCT of equity)
        if (deployed_today + cost) > total_equity * config.MAX_TOTAL_EXPOSURE_PCT:
            cap = total_equity * config.MAX_TOTAL_EXPOSURE_PCT
            return False, (f"Exposure cap: ${deployed_today + cost:.0f} "
                           f"> {int(config.MAX_TOTAL_EXPOSURE_PCT*100)}% equity (${cap:.0f})")

        # Cash/GFV: settled funds only
        if settled_cash < cost:
            return False, f"Insufficient settled cash (${settled_cash:.0f} < ${cost:.0f})"

        # T+1 daily capital cap
        if deployed_today + cost > config.MAX_DAILY_CAPITAL:
            return False, (f"Daily capital limit: ${deployed_today + cost:.0f} "
                           f"> ${config.MAX_DAILY_CAPITAL:.0f}")

        # Position count
        if num_positions >= config.MAX_CONCURRENT_POSITIONS:
            return False, f"Max concurrent positions ({config.MAX_CONCURRENT_POSITIONS}) reached"

        # Position size floor
        if cost < config.MIN_POSITION_SIZE:
            return False, f"Position ${cost:.0f} < min ${config.MIN_POSITION_SIZE:.0f}"

        # Rule 3: risk per trade ceiling
        if stop_loss and price > stop_loss:
            risk_dollars = (price - stop_loss) * qty
            if risk_dollars > config.MAX_RISK_PER_TRADE:
                return False, f"Risk ${risk_dollars:.0f} > hard ceiling ${config.MAX_RISK_PER_TRADE:.0f}"
            stop_pct = (price - stop_loss) / price
            if stop_pct > 0.04:
                return False, f"Stop {stop_pct:.1%} too wide — R:R destroyed"

        # Reward-to-risk minimum
        if reward_to_risk is not None and reward_to_risk < config.MIN_REWARD_TO_RISK:
            return False, f"R:R {reward_to_risk:.1f} < minimum {config.MIN_REWARD_TO_RISK}"

        # Signal confidence floor
        if signal_confidence < config.MIN_SIGNAL_CONFIDENCE:
            return False, (f"Signal confidence {signal_confidence}/10 "
                           f"< minimum {config.MIN_SIGNAL_CONFIDENCE}")

        # Volume confirmation — threshold relaxed in early window and for gap-and-go setups
        _vol_floor = min_vol_ratio_override if min_vol_ratio_override is not None else config.MIN_VOL_RATIO_ENTRY
        if vol_ratio is not None and vol_ratio < _vol_floor:
            return False, (f"vol_ratio {vol_ratio:.2f} < {_vol_floor:.2f} "
                           f"— insufficient volume")

        # Structural resistance proximity check: skip if price is within 0.5% of a key wall.
        # RSI is no longer a hard veto — institutions buy through high RSI when momentum is real.
        if key_levels and price > 0:
            nearest_res = key_levels.get("nearest_resistance")
            if nearest_res and nearest_res > price:
                distance_pct = (nearest_res - price) / price
                if distance_pct < 0.005:  # within 0.5% of resistance → poor R:R entry
                    return False, (f"Price within {distance_pct:.2%} of resistance ${nearest_res:.2f} "
                                   f"— insufficient headroom for R:R (RSI={rsi:.0f})")

        # Rule 1: bid-ask spread must be tight
        if spread_pct is not None and spread_pct > config.MAX_SPREAD_PCT:
            return False, (f"Spread {spread_pct:.4%} > max {config.MAX_SPREAD_PCT:.4%} "
                           f"(Rule 1: tight spread required)")

        return True, "OK"

    @staticmethod
    def check_portfolio_heat(positions_snapshot: list[dict], new_risk_dollars: float,
                             total_equity: float) -> tuple[bool, str]:
        """Ensure combined worst-case stop-out loss stays within the heat cap.

        Portfolio heat = sum of worst-case dollar losses if EVERY open stop triggers
        simultaneously (flash crash / gap-down scenario).

        Institutional rule: combined heat must never exceed MAX_PORTFOLIO_HEAT_PCT of equity.
        This is the key guard that individual-trade risk checks cannot catch.
        """
        max_heat      = total_equity * config.MAX_PORTFOLIO_HEAT_PCT
        current_heat  = 0.0
        heat_details  = []

        for p in positions_snapshot:
            ep  = float(p.get("entry_price", 0))
            sl  = float(p.get("stop_loss",   0))
            qty = float(p.get("qty",         0))
            if ep > 0 and sl > 0 and ep > sl and qty > 0:
                loss = (ep - sl) * qty
                current_heat += loss
                heat_details.append(f"{p['symbol']}=${loss:.0f}")

        total_heat = current_heat + new_risk_dollars
        if total_heat > max_heat:
            detail = ", ".join(heat_details) if heat_details else "none"
            return False, (f"Portfolio heat ${total_heat:.0f} > max ${max_heat:.0f} "
                           f"({config.MAX_PORTFOLIO_HEAT_PCT:.0%} of equity). "
                           f"Open position risks: [{detail}]. "
                           f"New trade risk: ${new_risk_dollars:.0f}. "
                           f"Reduce position sizes or wait for existing stops to tighten.")
        return True, f"Portfolio heat OK (${total_heat:.0f} / ${max_heat:.0f})"

    @staticmethod
    def approve_stop_update(symbol: str, new_stop: float,
                            current_stop: float) -> tuple[bool, str]:
        """Enforce Rule 5: stops only move in the direction of profit (never widen).

        Args:
            symbol: Ticker (used by the caller for logging; not read here).
            new_stop: Proposed new stop price.
            current_stop: Active stop price on the current position.

        Returns:
            Tuple of (approved: bool, reason: str).
        """
        # Stops only move in the direction of profit — never widen (Rule 5)
        if new_stop <= current_stop:
            return False, (f"Stop rejected: new {new_stop:.2f} ≤ current {current_stop:.2f} "
                           f"(rule 5 — never widen)")
        return True, "OK"

    @staticmethod
    def volatility_size_factor(atr: float, price: float) -> tuple[float, str]:
        """Map per-stock ATR to a position-size scaling factor (Rule 5).

        Higher volatility → smaller size so that dollar risk stays constant
        regardless of how wide the bars are.

        Args:
            atr: 14-period Average True Range for the stock.
            price: Current price of the stock.

        Returns:
            Tuple of (factor: float, regime_label: str). factor multiplies
            the base risk-sized share count computed by calc_qty().
        """
        if not atr or not price:
            return 1.0, "unknown"
        atr_pct = atr / price
        for threshold, factor, label in config.VOL_REGIME_THRESHOLDS:
            if atr_pct > threshold:
                return factor, label
        return 1.0, "normal"

    @staticmethod
    def calc_qty(price: float, stop_loss: float, settled_cash: float,
                 deployed_today: float, total_equity: float,
                 atr: float = 0.0, confidence: int = 9,
                 vix_factor: float = 1.0, kelly_factor: float = 1.0,
                 position_cap: float | None = None) -> float:
        """Compute the whole-share position size with four cascading adjustments.

        Sizing waterfall:
          1. Risk-size to target 0.75% of equity (hard ceiling $100 per trade).
          2. Apply per-stock ATR regime factor (high volatility → smaller position).
          3. Apply AI confidence scale (higher conviction → proportionally larger).
          4. Apply VIX regime factor (market-wide fear → shrink all positions).
          5. Cap at the minimum of: MAX_POSITION_SIZE, available settled cash,
             remaining daily capital headroom, and remaining exposure headroom.

        Args:
            price: Proposed entry price.
            stop_loss: Hard stop price; defines dollar risk per share.
            settled_cash: T+1-settled cash available for new buys.
            deployed_today: Dollar amount already committed this session.
            total_equity: Account equity at cycle start.
            atr: 14-period ATR; 0.0 triggers a percentage fallback.
            confidence: AI signal confidence (1–10); maps to a size scale factor.
            vix_factor: Regime-based multiplier from market_guard.get_vix_regime().
            kelly_factor: Half-Kelly multiplier from historical win rate/R:R (default 1.0).

        Returns:
            Whole number of shares (float with no fractional part). Returns 0.0
            if risk_per_share <= 0 or the available capital is exhausted.
        """
        # ATR-aware stop fallback: use 1.5× ATR when no stop is provided,
        # falling back to the percentage floor if ATR is unavailable.
        if not stop_loss or stop_loss >= price:
            if atr > 0:
                stop_loss = price - max(atr * config.ATR_STOP_MULTIPLIER,
                                        price * config.DEFAULT_STOP_LOSS_PCT)
            else:
                stop_loss = price * (1 - config.DEFAULT_STOP_LOSS_PCT)

        risk_per_share = price - stop_loss
        if risk_per_share <= 0:
            return 0.0

        # Base risk-sized shares
        target_risk    = min(total_equity * config.MAX_RISK_PER_TRADE_PCT,
                             config.MAX_RISK_PER_TRADE)
        shares_by_risk = target_risk / risk_per_share

        # Apply volatility regime (Rule 5: size ∝ 1/vol)
        vol_factor, regime = RiskManager.volatility_size_factor(atr, price)
        shares_by_risk *= vol_factor
        if regime not in ("normal", "unknown"):
            log.info("Vol regime '%s' → size factor %.2f", regime, vol_factor)

        # Apply confidence scale (higher conviction → proportionally larger size)
        conf_factor = config.CONFIDENCE_SIZE_SCALE.get(confidence, 0.55)
        shares_by_risk *= conf_factor
        if conf_factor != 1.0:
            log.info("Confidence %d/10 → size factor %.2f", confidence, conf_factor)

        # Apply VIX regime factor (market-wide fear → shrink all positions)
        if vix_factor != 1.0:
            shares_by_risk *= vix_factor
            log.info("VIX regime → size factor %.2f applied", vix_factor)

        # Apply Kelly Criterion (half-Kelly, pre-computed by caller from DB win rate + R:R)
        if kelly_factor != 1.0:
            shares_by_risk *= max(0.25, min(kelly_factor, 1.5))
            log.info("Kelly factor %.3f applied to sizing", kelly_factor)

        # Cap by position size limit — use dynamic cap if provided, else config default
        effective_cap  = position_cap if position_cap is not None else config.MAX_POSITION_SIZE
        shares_by_size = effective_cap / price

        # Cap by available capital headroom
        available = min(
            settled_cash,
            config.MAX_DAILY_CAPITAL - deployed_today,
            total_equity * config.MAX_TOTAL_EXPOSURE_PCT - deployed_today,
        )
        shares_by_cash = available / price if available > 0 else 0

        qty = min(shares_by_risk, shares_by_size, shares_by_cash)
        return float(max(int(qty), 0))

    @staticmethod
    def compute_stop_take_profit(price: float, atr: float,
                                  key_levels: dict | None = None) -> tuple[float, float]:
        """ATR-based stop and take-profit with minimum R:R guarantee.

        If key_levels provides a resistance level 1–5% above entry that gives R:R >= 2,
        use that level (minus 0.2% buffer) as TP — exits before the wall, not into it.
        """
        atr           = atr or price * 0.01
        stop_distance = max(atr * 1.5, price * config.DEFAULT_STOP_LOSS_PCT)
        stop          = round(price - stop_distance, 2)
        atr_tp        = round(price + max(stop_distance * config.MIN_REWARD_TO_RISK,
                                          price * config.DEFAULT_TAKE_PROFIT_PCT), 2)

        if key_levels:
            nearest_res = key_levels.get("nearest_resistance")
            if nearest_res and nearest_res > price and stop_distance > 0:
                res_pct  = (nearest_res - price) / price
                level_rr = (nearest_res - price) / stop_distance
                if 0.01 <= res_pct <= 0.05 and level_rr >= config.MIN_REWARD_TO_RISK:
                    level_tp = round(nearest_res * 0.998, 2)
                    log.info("Level-based TP: resistance=%.2f → TP=%.2f (R:R=%.1f) "
                             "vs ATR TP=%.2f", nearest_res, level_tp, level_rr, atr_tp)
                    return stop, level_tp

        return stop, atr_tp

    @staticmethod
    def is_too_volatile(atr: float, price: float) -> bool:
        """Return True when ATR/price exceeds the absolute volatility ceiling.

        Stocks with ATR > MAX_TRADEABLE_ATR_PCT are skipped entirely — sizing
        math breaks down and stops would need to be unreasonably wide.

        Args:
            atr: 14-period ATR for the stock.
            price: Current price of the stock.

        Returns:
            True if the stock is too volatile to trade safely; False otherwise.
            Also returns False when atr or price is zero (fail-open).
        """
        if not atr or not price:
            return False
        return (atr / price) > config.MAX_TRADEABLE_ATR_PCT
