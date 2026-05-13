"""
Options risk manager: enforces defined-risk entry gates, contract sizing, and
portfolio-level Greeks limits for the options trading system.

Risk philosophy:
  - Every trade must be defined-risk (spreads only, no naked shorts).
  - Premium at risk is tracked as a daily aggregate and per-trade cap.
  - Portfolio delta and vega are capped so no single underlying move or IV spike
    can exceed the daily drawdown budget.
  - The 200% credit-received stop and 50% take-profit rule are enforced here at
    the position monitoring layer, not just at strategy selection.
  - A consecutive-loss guard mirrors the equity revenge-trade guard: widening
    losses this session elevate the signal bar for new entries.
"""

from __future__ import annotations

from core.database import log
import config


class OptionsRiskManager:
    """
    Defined-risk entry gates, contract sizing, and portfolio Greeks limits
    for the options trading engine.

    Designed to be instantiated once and shared across the executor, position
    monitor, and trade cycle via dependency injection.
    """

    # ── Entry approval ─────────────────────────────────────────────────────────

    @staticmethod
    def approve_entry(
        symbol:               str,
        strategy_type:        str,
        max_loss_dollars:     float,
        daily_premium_at_risk: float,
        open_positions_count: int,
        daily_pnl:            float,
        total_equity:         float,
        portfolio_delta:      float,
        portfolio_vega:       float,
        new_position_delta:   float,
        new_position_vega:    float,
        iv_rank:              float,
        vrp:                  float,
        signal_score:         float,
        has_earnings_soon:    bool = False,
        consecutive_losses:   int  = 0,
    ) -> tuple[bool, str]:
        """
        Validate a new options entry against all risk rules.

        Args:
            symbol:                Underlying ticker (used in veto messages).
            strategy_type:         Strategy label — determines which rules apply.
            max_loss_dollars:      Worst-case dollar loss for the proposed position.
            daily_premium_at_risk: Sum of max_loss across all open options positions.
            open_positions_count:  Number of currently open options positions.
            daily_pnl:             Realized + unrealized P&L for today.
            total_equity:          Account equity (for percentage-based limits).
            portfolio_delta:       Current absolute portfolio delta.
            portfolio_vega:        Current absolute portfolio vega.
            new_position_delta:    Absolute delta of the proposed new position.
            new_position_vega:     Absolute vega of the proposed new position.
            iv_rank:               Current IV Rank for the underlying (0–100).
            vrp:                   Volatility risk premium in volatility points.
            signal_score:          Underlying directional signal score (0–10).
            has_earnings_soon:     True if earnings fall within the credit blackout window.
            consecutive_losses:    Number of consecutive losing options trades today.

        Returns:
            (True, "OK") if all gates pass; (False, veto_reason) if any gate fails.
        """
        is_credit = strategy_type in (
            "credit_put_spread", "credit_call_spread", "iron_condor"
        )
        is_debit = strategy_type in (
            "debit_call_spread", "debit_put_spread",
            "zero_dte_call_spread", "zero_dte_put_spread",
        )

        # ── Daily drawdown halt ──────────────────────────────────────────────
        if daily_pnl <= -config.DAILY_DRAWDOWN_LIMIT:
            return False, (
                f"Daily drawdown limit hit (${daily_pnl:.0f} ≤ "
                f"-${config.DAILY_DRAWDOWN_LIMIT:.0f}) — trading halted for the day"
            )

        # ── Daily premium-at-risk cap ────────────────────────────────────────
        if (daily_premium_at_risk + max_loss_dollars) > config.MAX_DAILY_PREMIUM_AT_RISK:
            return False, (
                f"Daily premium at risk ${daily_premium_at_risk + max_loss_dollars:.0f} "
                f"> cap ${config.MAX_DAILY_PREMIUM_AT_RISK:.0f} — no new entries until "
                f"existing positions close"
            )

        # ── Per-trade max loss ───────────────────────────────────────────────
        if max_loss_dollars > config.MAX_PREMIUM_PER_TRADE:
            return False, (
                f"Trade max loss ${max_loss_dollars:.0f} exceeds per-trade limit "
                f"${config.MAX_PREMIUM_PER_TRADE:.0f} — size down or widen spread"
            )

        # ── Concurrent positions cap ─────────────────────────────────────────
        if open_positions_count >= config.MAX_OPEN_OPTIONS_POSITIONS:
            return False, (
                f"Max concurrent options positions "
                f"({config.MAX_OPEN_OPTIONS_POSITIONS}) reached"
            )

        # ── Portfolio delta cap ──────────────────────────────────────────────
        combined_delta = abs(portfolio_delta + new_position_delta)
        if combined_delta > config.MAX_PORTFOLIO_DELTA:
            return False, (
                f"Portfolio delta {combined_delta:.1f} would exceed "
                f"max {config.MAX_PORTFOLIO_DELTA} — portfolio already directionally biased"
            )

        # ── Portfolio vega cap ───────────────────────────────────────────────
        combined_vega = abs(portfolio_vega + new_position_vega)
        if combined_vega > config.MAX_PORTFOLIO_VEGA:
            return False, (
                f"Portfolio vega {combined_vega:.1f} would exceed "
                f"max {config.MAX_PORTFOLIO_VEGA} — IV sensitivity already high"
            )

        # ── Credit-strategy-specific gates ──────────────────────────────────
        if is_credit:
            if has_earnings_soon:
                return False, (
                    f"Credit spread/condor on {symbol} blocked: earnings within "
                    f"{config.EARNINGS_BLACKOUT_DAYS_CREDIT} days — IV crush timing is "
                    f"unpredictable, skip this cycle"
                )
            if vrp < config.MIN_VRP_TO_SELL:
                return False, (
                    f"VRP {vrp:.1f} pts below minimum {config.MIN_VRP_TO_SELL} — "
                    f"no statistical edge for premium sellers at this IV level"
                )
            if iv_rank < config.IV_RANK_HIGH_THRESHOLD:
                return False, (
                    f"IV Rank {iv_rank:.0f} < {config.IV_RANK_HIGH_THRESHOLD} — "
                    f"IV not rich enough to justify selling premium"
                )

        # ── Debit-strategy-specific gates ───────────────────────────────────
        if is_debit:
            if signal_score < config.DEBIT_MIN_SIGNAL_SCORE:
                return False, (
                    f"Debit spread on {symbol} blocked: signal {signal_score:.1f} "
                    f"< minimum {config.DEBIT_MIN_SIGNAL_SCORE} required for debit entry"
                )
            if iv_rank > config.IV_RANK_LOW_THRESHOLD + 5:
                return False, (
                    f"IV Rank {iv_rank:.0f} too high for debit spread — paying "
                    f"elevated premium with poor edge"
                )

        # ── Revenge-trade guard ──────────────────────────────────────────────
        if consecutive_losses >= 3:
            required = config.DEBIT_MIN_SIGNAL_SCORE + 2.0
            if signal_score < required:
                return False, (
                    f"Revenge-trade guard: {consecutive_losses} consecutive losses this "
                    f"session. Require signal {required:.0f}+ (have {signal_score:.1f}). "
                    f"Stand aside until edge clearly re-establishes."
                )

        elif consecutive_losses >= 2:
            required = config.DEBIT_MIN_SIGNAL_SCORE + 1.0
            if signal_score < required:
                return False, (
                    f"Revenge-trade guard: {consecutive_losses} consecutive losses. "
                    f"Require signal {required:.0f}+ for next entry (have {signal_score:.1f})."
                )

        return True, "OK"

    # ── Position sizing ────────────────────────────────────────────────────────

    @staticmethod
    def size_position(
        strategy_type:    str,
        max_loss_per_contract: float,
        total_equity:     float,
        daily_pnl:        float,
        vix_level:        float,
        available_capital: float,
    ) -> int:
        """
        Calculate the number of contracts to trade for a new options position.

        Sizing waterfall:
          1. Risk-size to MAX_PREMIUM_PER_TRADE (never more than this per trade).
          2. Apply VIX regime scaling (high vol → smaller debit, neutral for credits).
          3. Apply daily-loss penalty (losing day → 50% size reduction).
          4. Floor at 1 contract minimum; cap at MAX_CONTRACTS_PER_TRADE.

        Args:
            strategy_type:         Strategy label (determines VIX scaling direction).
            max_loss_per_contract: Worst-case loss per contract in dollars (= spread
                                   width × 100 for spreads).
            total_equity:          Account equity for percentage-based limits.
            daily_pnl:             Today's realized P&L (negative = losing day).
            vix_level:             Current VIX or realized vol proxy.
            available_capital:     Buying power available for the trade.

        Returns:
            Number of contracts (integer ≥ 1), or 0 if max_loss_per_contract <= 0
            or available capital is exhausted.
        """
        if max_loss_per_contract <= 0:
            return 0

        is_debit = strategy_type in (
            "debit_call_spread", "debit_put_spread",
            "zero_dte_call_spread", "zero_dte_put_spread",
        )

        # Base contracts from premium risk budget
        base_risk  = min(config.MAX_PREMIUM_PER_TRADE, available_capital)
        contracts  = int(base_risk / max_loss_per_contract)

        # VIX scaling: high VIX penalizes debit buyers (paying inflated premium),
        # but is neutral-to-favorable for credit sellers (selling rich premium).
        if is_debit and vix_level > 25:
            vix_scale = max(0.5, 1.0 - (vix_level - 25) * 0.02)
            contracts = max(1, int(contracts * vix_scale))
            log.info("VIX %.1f → debit size scaled by %.2f", vix_level, vix_scale)

        # Daily-loss penalty: after losing 1% of equity, cut all new sizes by half
        loss_threshold = total_equity * 0.01
        if daily_pnl < -loss_threshold:
            contracts = max(1, contracts // 2)
            log.info("Daily-loss penalty active (P&L=%.0f) → half-size entry", daily_pnl)

        # Hard cap
        contracts = min(contracts, config.MAX_CONTRACTS_PER_TRADE)

        return max(0, contracts)

    # ── Portfolio Greeks monitoring ────────────────────────────────────────────

    @staticmethod
    def check_portfolio_greeks(
        positions: list[dict],
    ) -> tuple[bool, str]:
        """
        Verify that aggregate portfolio Greeks are within configured limits.

        Computed from the stored net_delta and net_vega in each open position.
        Should be called before every new entry and after every position close.

        Args:
            positions: List of open options position dicts from the database,
                       each containing 'net_delta' and 'net_vega' fields.

        Returns:
            (True, summary_string) if within limits; (False, veto_reason) if exceeded.
        """
        total_delta = sum(float(p.get("net_delta", 0)) for p in positions)
        total_vega  = sum(float(p.get("net_vega",  0)) for p in positions)
        abs_delta   = abs(total_delta)
        abs_vega    = abs(total_vega)

        if abs_delta > config.MAX_PORTFOLIO_DELTA:
            return False, (
                f"Portfolio delta {abs_delta:.1f} exceeds limit "
                f"{config.MAX_PORTFOLIO_DELTA} — hedge or close a directional position"
            )
        if abs_vega > config.MAX_PORTFOLIO_VEGA:
            return False, (
                f"Portfolio vega {abs_vega:.1f} exceeds limit "
                f"{config.MAX_PORTFOLIO_VEGA} — too much IV exposure across positions"
            )

        return True, (
            f"Portfolio Greeks OK: Δ={total_delta:+.1f} ν={total_vega:+.1f}"
        )

    # ── Daily premium-at-risk aggregation ─────────────────────────────────────

    @staticmethod
    def compute_daily_premium_at_risk(positions: list[dict]) -> float:
        """
        Sum the max_loss across all open options positions.

        This is the aggregate worst-case loss if every open position hits its
        maximum loss simultaneously (gap scenario). Used by approve_entry() to
        enforce the daily premium-at-risk cap.

        Args:
            positions: List of open options position dicts from the database,
                       each containing a 'max_loss' field.

        Returns:
            Total premium at risk in dollars.
        """
        return sum(float(p.get("max_loss", 0)) for p in positions)

    # ── Exit rule checks ───────────────────────────────────────────────────────

    @staticmethod
    def should_take_profit(
        entry_premium: float,
        current_premium: float,
        is_credit: bool,
    ) -> tuple[bool, str]:
        """
        Apply the 50% max-profit exit rule (Tastytrade-validated).

        For credit spreads: exit when current premium falls to 50% of entry credit.
        For debit spreads:  exit when current premium rises to 150% of entry debit
                            (equivalent 50% of max profit for a 2:1 spread).

        The 50% rule dramatically improves win rate by locking in gains before
        gamma risk accelerates near expiry.

        Args:
            entry_premium:   Premium received (credit) or paid (debit) at entry.
            current_premium: Current mark price of the spread (per share).
            is_credit:       True for credit spreads/condors; False for debit.

        Returns:
            (True, reason) if the 50% rule triggers; (False, "") otherwise.
        """
        if entry_premium <= 0:
            return False, ""

        if is_credit:
            # For credit: profit when we can buy back at 50% of what we sold for
            target_buyback = entry_premium * config.CREDIT_TAKE_PROFIT_PCT
            if current_premium <= target_buyback:
                profit_pct = (entry_premium - current_premium) / entry_premium
                return True, (
                    f"50% profit rule: bought back at {current_premium:.2f} "
                    f"vs entry {entry_premium:.2f} ({profit_pct:.0%} of credit captured)"
                )
        else:
            # For debit: profit when spread value is 1.5× what we paid
            target_value = entry_premium * 1.5
            if current_premium >= target_value:
                profit_pct = (current_premium - entry_premium) / entry_premium
                return True, (
                    f"50% profit rule: spread value {current_premium:.2f} "
                    f"vs entry {entry_premium:.2f} ({profit_pct:.0%} gain)"
                )

        return False, ""

    @staticmethod
    def should_stop_loss(
        entry_premium: float,
        current_premium: float,
        is_credit: bool,
    ) -> tuple[bool, str]:
        """
        Apply the 200% credit-received stop rule for credit strategies.

        For credit spreads: exit when the spread has widened to 3× the credit
        received (loss = 2× credit = 200% of original premium received).
        This limits the loss to roughly 2:1 vs the max-profit scenario.

        For debit spreads: exit when 50% of the premium paid is lost.
        (Debit positions have more natural loss containment via defined risk.)

        Args:
            entry_premium:   Premium received (credit) or paid (debit) at entry.
            current_premium: Current mark price of the spread (per share).
            is_credit:       True for credit spreads/condors; False for debit.

        Returns:
            (True, reason) if the stop triggers; (False, "") otherwise.
        """
        if entry_premium <= 0:
            return False, ""

        if is_credit:
            # Current loss = current_premium − entry_premium (we owe more than we received)
            current_loss = current_premium - entry_premium
            stop_threshold = entry_premium * config.CREDIT_STOP_LOSS_MULTIPLIER
            if current_loss >= stop_threshold:
                return True, (
                    f"200% stop triggered: current spread {current_premium:.2f} vs "
                    f"entry {entry_premium:.2f} (loss = {current_loss:.2f} = "
                    f"{current_loss / entry_premium:.0%} of credit received)"
                )
        else:
            # Debit: stop when 50% of what we paid is gone
            current_loss = entry_premium - current_premium
            stop_pct     = 0.50
            if current_loss >= entry_premium * stop_pct:
                return True, (
                    f"Debit stop triggered: spread value {current_premium:.2f} vs "
                    f"entry {entry_premium:.2f} ({current_loss / entry_premium:.0%} lost)"
                )

        return False, ""

    @staticmethod
    def should_exit_by_dte(dte_remaining: int, strategy_type: str) -> tuple[bool, str]:
        """
        Apply DTE-based exit rules to avoid gamma risk near expiry.

        Credits: close at DTE ≤ CREDIT_CLOSE_DTE_DAYS (default 7) to avoid the
        explosive gamma risk that can turn a winner into a loser overnight.
        Debits:  close at DTE ≤ DEBIT_CLOSE_DTE_DAYS (default 3) — time decay
        accelerates, theta burn exceeds any remaining directional edge.

        Args:
            dte_remaining: Calendar days remaining to expiration.
            strategy_type: Strategy label string.

        Returns:
            (True, reason) if DTE exit rule triggers; (False, "") otherwise.
        """
        is_credit = strategy_type in (
            "credit_put_spread", "credit_call_spread", "iron_condor"
        )
        threshold = config.CREDIT_CLOSE_DTE_DAYS if is_credit else config.DEBIT_CLOSE_DTE_DAYS

        if dte_remaining <= threshold:
            return True, (
                f"DTE exit: {dte_remaining} days remaining ≤ "
                f"{threshold}-day threshold for {strategy_type} — "
                f"closing to avoid gamma risk"
            )
        return False, ""

    @staticmethod
    def should_exit_by_delta(
        short_leg_delta_abs: float,
        strategy_type: str,
    ) -> tuple[bool, str]:
        """
        Exit when the short leg's delta approaches 0.10 (deep ITM territory).

        When the short leg becomes significantly ITM, the spread's delta profile
        changes — we are fighting against growing intrinsic value instead of
        collecting time decay. Getting out early preserves capital for better
        opportunities.

        Args:
            short_leg_delta_abs: Absolute delta of the short leg (0.0–1.0).
            strategy_type:       Strategy label string.

        Returns:
            (True, reason) if delta exit triggers; (False, "") otherwise.
        """
        # For credit spreads: short leg going deeply ITM is an emergency exit
        if strategy_type in ("credit_put_spread", "credit_call_spread"):
            if short_leg_delta_abs > 0.75:
                return True, (
                    f"Delta emergency exit: short leg delta {short_leg_delta_abs:.2f} > 0.75 "
                    f"— short leg deeply ITM, cutting losses before expiry risk"
                )
        # For iron condors: either short wing going ITM triggers exit
        if strategy_type == "iron_condor":
            if short_leg_delta_abs > 0.65:
                return True, (
                    f"Iron condor delta exit: short wing delta {short_leg_delta_abs:.2f} > 0.65 "
                    f"— one wing breached, close to avoid max loss"
                )
        return False, ""

    # ── Max-loss computation helpers ──────────────────────────────────────────

    @staticmethod
    def compute_max_loss_spread(
        spread_width: float,
        net_premium:  float,
        contracts:    int,
    ) -> float:
        """
        Compute the worst-case dollar loss for a vertical spread.

        For credit spreads: max_loss = (spread_width − net_credit) × 100 × contracts
        For debit spreads:  max_loss = net_debit × 100 × contracts

        Args:
            spread_width: Distance between strikes in dollars (e.g. 5.0 for a $5-wide spread).
            net_premium:  Net credit received (positive) or debit paid (positive).
                          Use the absolute value; the sign is determined by context.
            contracts:    Number of contracts.

        Returns:
            Maximum dollar loss (always positive).
        """
        multiplier = contracts * 100
        max_loss   = (spread_width - abs(net_premium)) * multiplier
        return round(max(0.0, max_loss), 2)

    @staticmethod
    def compute_max_loss_iron_condor(
        spread_width: float,
        net_credit:   float,
        contracts:    int,
    ) -> float:
        """
        Compute worst-case dollar loss for an iron condor.

        Iron condor max loss occurs when the underlying closes beyond one of the
        long wings at expiry: max_loss = (wider_wing_width − net_credit) × 100 × contracts.

        Args:
            spread_width: Width of the wider spread wing in dollars.
            net_credit:   Total net credit received from both spread legs.
            contracts:    Number of iron condors.

        Returns:
            Maximum dollar loss (always positive).
        """
        return OptionsRiskManager.compute_max_loss_spread(
            spread_width, net_credit, contracts
        )
