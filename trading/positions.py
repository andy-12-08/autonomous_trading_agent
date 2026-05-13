"""
Options position manager: monitors every open options position every two minutes,
applies mechanically-enforced exit rules, and handles EOD flattening.

Exit rule priority (in order of check):
  1. EOD gate        — 3:45 PM ET → close everything
  2. DTE exit        — ≤ 7 DTE for credit / ≤ 3 DTE for debit
  3. 50% profit rule — Tastytrade-validated; locks in gains before gamma accelerates
  4. 200% stop rule  — credit strategies: current mark ≥ 3× credit received
  5. Delta emergency — short leg delta > 0.75 (deeply ITM → run, don't walk)
  6. Daily drawdown  — effective P&L ≤ −DAILY_DRAWDOWN_LIMIT → flatten all

This module does NOT contain entry logic; that lives in executor.py.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

import config
from analysis.greeks_engine import GreeksEngine
from analysis.options_strategy_selector import (
    CREDIT_PUT_SPREAD, CREDIT_CALL_SPREAD, IRON_CONDOR,
    DEBIT_CALL_SPREAD, DEBIT_PUT_SPREAD,
    ZERO_DTE_CALL, ZERO_DTE_PUT,
)
from core.database import log
from risk.options_risk import OptionsRiskManager


class OptionsPositionsMixin:
    """
    Two-minute scheduler job: monitor options positions and enforce exit rules.

    Expects these attributes on `self` (set by TradingOrchestrator):
      broker, database, notifier, iv_analyzer, _daily_pnl,
      _ET, _state_lock, _broker_lock, _eod_done
    """

    def monitor_options_positions(self) -> None:
        """
        Main 2-minute scheduler entry: check all open positions against exit rules.

        Returns:
            None.
        """
        now   = datetime.now(self._ET)
        hour  = now.hour
        minute = now.minute

        log.info("====[ OPTIONS POSITION MONITOR | %s ]====", now.strftime("%H:%M:%S"))

        # EOD gate: flatten all options positions at configured close time
        close_min = config.MARKET_CLOSE_HOUR * 60 + config.MARKET_CLOSE_MIN
        cur_min   = hour * 60 + minute

        if cur_min >= close_min and not self._eod_done:
            log.info("EOD gate: closing all options positions at %s", now.strftime("%H:%M"))
            self.eod_close_all_options()
            self._eod_done = True
            return

        if not self.broker.is_market_open():
            log.info("Market closed — skipping options position monitor")
            return

        open_positions = self.database.get_open_options_positions()
        if not open_positions:
            log.info("No open options positions to monitor")
            return

        # Fetch account state once per cycle
        try:
            account = self.broker.get_account()
            equity  = float(getattr(account, "equity", None) or config.ACCOUNT_SIZE)
        except Exception:
            equity  = config.ACCOUNT_SIZE

        total_pnl = sum(float(p.get("current_pnl", 0)) for p in open_positions)
        effective_pnl = self._daily_pnl + total_pnl

        # Daily drawdown halt: close everything immediately
        if effective_pnl <= -config.DAILY_DRAWDOWN_LIMIT:
            log.warning(
                "DRAWDOWN HALT: effective P&L $%.0f ≤ -$%.0f — flattening all options",
                effective_pnl, config.DAILY_DRAWDOWN_LIMIT,
            )
            self.eod_close_all_options(reason="drawdown_halt")
            return

        for pos in open_positions:
            try:
                self._check_position_exits(pos, equity)
            except Exception as exc:
                log.error("Options position monitor error %s: %s",
                          pos.get("position_id", "?"), exc)

        log.info("Options monitor done — %d position(s) reviewed", len(open_positions))

    def _check_position_exits(self, pos: dict, equity: float) -> None:
        """
        Evaluate all exit rules for a single open position.

        Args:
            pos:    Open options position dict from the database.
            equity: Current account equity for P&L calculations.

        Returns:
            None.
        """
        position_id   = pos["position_id"]
        symbol        = pos["symbol"]
        strategy      = pos["strategy_type"]
        contracts     = int(pos.get("contracts", 1))
        entry_premium = float(pos.get("entry_premium", 0))
        max_profit    = float(pos.get("max_profit",    0))
        max_loss      = float(pos.get("max_loss",      1))
        expiry        = pos.get("expiry", "")

        is_credit = strategy in (CREDIT_PUT_SPREAD, CREDIT_CALL_SPREAD, IRON_CONDOR)

        # ── Compute DTE remaining ─────────────────────────────────────────────
        dte_remaining = self._compute_dte(expiry)

        # ── Rule 1: DTE exit ──────────────────────────────────────────────────
        should_exit_dte, dte_reason = OptionsRiskManager.should_exit_by_dte(
            dte_remaining, strategy)
        if should_exit_dte:
            self._close_position(pos, dte_reason, equity, "dte_exit")
            return

        # ── Fetch current spread mark price ───────────────────────────────────
        current_premium = self._get_current_spread_premium(pos, symbol)
        if current_premium is None:
            log.debug("Could not fetch current premium for %s — skipping this cycle",
                      position_id)
            return

        # Update live P&L in the database
        multiplier  = contracts * 100
        if is_credit:
            current_pnl = (entry_premium - current_premium) * multiplier
        else:
            current_pnl = (current_premium - entry_premium) * multiplier
        self.database.update_options_position_pnl(position_id, current_pnl)

        # ── Rule 2: 50% profit exit ───────────────────────────────────────────
        should_tp, tp_reason = OptionsRiskManager.should_take_profit(
            entry_premium, current_premium, is_credit)
        if should_tp:
            self._close_position(pos, tp_reason, equity, "50pct_profit",
                                 realized_pnl=current_pnl)
            return

        # ── Rule 3: 200% stop loss ────────────────────────────────────────────
        should_sl, sl_reason = OptionsRiskManager.should_stop_loss(
            entry_premium, current_premium, is_credit)
        if should_sl:
            self._close_position(pos, sl_reason, equity, "stop_loss",
                                 realized_pnl=current_pnl)
            return

        # ── Rule 4: Delta emergency exit ──────────────────────────────────────
        short_delta_abs = self._get_short_leg_delta_abs(pos, symbol)
        if short_delta_abs is not None:
            should_exit_delta, delta_reason = OptionsRiskManager.should_exit_by_delta(
                short_delta_abs, strategy)
            if should_exit_delta:
                self._close_position(pos, delta_reason, equity, "delta_exit",
                                     realized_pnl=current_pnl)
                return

        log.debug("Position %s OK: DTE=%d pnl=%+.2f premium=%.2f (entry=%.2f)",
                  symbol, dte_remaining, current_pnl, current_premium, entry_premium)

    # ── Position closing ──────────────────────────────────────────────────────

    def _close_position(
        self,
        pos:          dict,
        reason:       str,
        equity:       float,
        close_reason: str,
        realized_pnl: float = 0.0,
    ) -> None:
        """
        Close all legs of an options position and record the outcome.

        Args:
            pos:          Open position dict from the database.
            reason:       Human-readable close reason for logging and alerts.
            equity:       Account equity at close time.
            close_reason: Short label for the close_reason DB column.
            realized_pnl: Computed P&L to record (0 if unavailable).

        Returns:
            None.
        """
        strategy    = pos["strategy_type"]
        symbol      = pos["symbol"]
        contracts   = int(pos.get("contracts", 1))
        position_id = pos["position_id"]

        log.info("CLOSE %s %s x%d: %s", symbol, strategy, contracts, reason[:80])

        success = False
        with self._broker_lock:
            success = self._submit_close_orders(pos)

        if not success:
            log.warning("Close order failed for %s — will retry on next cycle", position_id)
            return

        # Persist outcome
        self.database.close_options_position(position_id, realized_pnl, close_reason)
        self.database.record_options_decision(
            symbol        = symbol,
            action        = "CLOSE",
            strategy_type = strategy,
            position_id   = position_id,
            rationale     = reason,
            max_loss      = float(pos.get("max_loss", 0)),
        )

        with self._state_lock:
            self._daily_pnl += realized_pnl

        # Alert
        entry_premium = float(pos.get("entry_premium", 0))
        max_profit    = float(pos.get("max_profit",    0))
        pnl_pct       = (realized_pnl / abs(float(pos.get("max_loss", 1))) * 100
                         if pos.get("max_loss") else 0)
        self.notifier.send_options_close_alert(
            strategy_type = strategy,
            symbol        = symbol,
            contracts     = contracts,
            realized_pnl  = realized_pnl,
            pnl_pct       = pnl_pct,
            close_reason  = close_reason,
            entry_premium = entry_premium,
            max_profit    = max_profit,
            equity        = equity,
            daily_pnl     = self._daily_pnl,
            rationale     = reason,
        )

    def _submit_close_orders(self, pos: dict) -> bool:
        """
        Submit the appropriate closing orders for the given position.

        For spreads: close both legs simultaneously.
        For iron condors: close all four legs.

        Args:
            pos: Open position dict from the database.

        Returns:
            True if all close orders were submitted successfully; False otherwise.
        """
        strategy  = pos["strategy_type"]
        contracts = int(pos.get("contracts", 1))

        if strategy == IRON_CONDOR:
            # Close put spread
            put_ok = self.broker.close_spread_position(
                long_symbol  = pos.get("put_long_symbol",   ""),
                short_symbol = pos.get("put_short_symbol",  ""),
                contracts    = contracts,
            )
            # Close call spread regardless of put result (avoid partial naked)
            call_ok = self.broker.close_spread_position(
                long_symbol  = pos.get("call_long_symbol",  ""),
                short_symbol = pos.get("call_short_symbol", ""),
                contracts    = contracts,
            )
            return put_ok and call_ok

        # Vertical spreads (all other strategies)
        long_sym  = pos.get("long_symbol",  "")
        short_sym = pos.get("short_symbol", "")

        if not long_sym or not short_sym:
            log.warning("Missing leg symbols for position %s — cannot close",
                        pos.get("position_id"))
            return False

        return self.broker.close_spread_position(long_sym, short_sym, contracts)

    # ── EOD flattening ────────────────────────────────────────────────────────

    def eod_close_all_options(self, reason: str = "eod_close") -> None:
        """
        Force-close all open options positions at end of day.

        Uses market orders to guarantee flat by market close.
        Called at 3:45 PM ET or on drawdown halt.

        Args:
            reason: Close reason label to store in the database.

        Returns:
            None.
        """
        open_positions = self.database.get_open_options_positions()
        if not open_positions:
            log.info("EOD close: no open options positions")
            return

        log.info("EOD close: flattening %d options position(s)", len(open_positions))

        try:
            account = self.broker.get_account()
            equity  = float(getattr(account, "equity", None) or config.ACCOUNT_SIZE)
        except Exception:
            equity  = config.ACCOUNT_SIZE

        for pos in open_positions:
            entry_premium = float(pos.get("entry_premium", 0))
            is_credit     = pos["strategy_type"] in (CREDIT_PUT_SPREAD, CREDIT_CALL_SPREAD, IRON_CONDOR)
            current_prem  = self._get_current_spread_premium(pos, pos["symbol"])

            if current_prem is not None and entry_premium > 0:
                contracts  = int(pos.get("contracts", 1))
                multiplier = contracts * 100
                realized   = ((entry_premium - current_prem) if is_credit
                              else (current_prem - entry_premium)) * multiplier
            else:
                realized = 0.0

            self._close_position(
                pos          = pos,
                reason       = f"EOD flatten: {reason}",
                equity       = equity,
                close_reason = reason,
                realized_pnl = realized,
            )

        log.info("EOD close complete — %d position(s) flattened", len(open_positions))

    # ── Live premium estimation ───────────────────────────────────────────────

    def _get_current_spread_premium(
        self, pos: dict, symbol: str,
    ) -> Optional[float]:
        """
        Estimate the current mark price of a spread position.

        Attempts to fetch live quotes for both legs. Falls back to a
        Black-Scholes approximation using current spot and IV if quotes fail.

        Args:
            pos:    Open position dict.
            symbol: Underlying ticker.

        Returns:
            Current spread premium per share, or None on complete failure.
        """
        strategy  = pos["strategy_type"]
        long_sym  = pos.get("long_symbol",  "")
        short_sym = pos.get("short_symbol", "")
        is_iron_condor = (strategy == IRON_CONDOR)

        try:
            # Attempt live quotes from broker
            if not is_iron_condor and long_sym and short_sym:
                long_quote  = self.broker.get_latest_quote(long_sym)
                short_quote = self.broker.get_latest_quote(short_sym)
                if long_quote and short_quote:
                    long_mid  = (long_quote.get("bid",  0) + long_quote.get("ask",  0)) / 2
                    short_mid = (short_quote.get("bid", 0) + short_quote.get("ask", 0)) / 2
                    is_credit = strategy in (CREDIT_PUT_SPREAD, CREDIT_CALL_SPREAD)
                    if is_credit:
                        return round(short_mid - long_mid, 3)
                    else:
                        return round(long_mid - short_mid, 3)

            if is_iron_condor:
                # Iron condor: sum the two spreads' values
                total = 0.0
                for put_sym, call_sym in (
                    (pos.get("put_long_symbol"),   pos.get("put_short_symbol")),
                    (pos.get("call_long_symbol"),  pos.get("call_short_symbol")),
                ):
                    if put_sym and call_sym:
                        long_q  = self.broker.get_latest_quote(put_sym or call_sym)
                        short_q = self.broker.get_latest_quote(call_sym or put_sym)
                        if long_q and short_q:
                            total += ((short_q.get("ask", 0) + short_q.get("bid", 0)) / 2
                                    - (long_q.get("bid",  0) + long_q.get("ask",  0)) / 2)
                if total > 0:
                    return round(total, 3)

        except Exception as exc:
            log.debug("Live quote failed for %s: %s — using BS fallback", symbol, exc)

        # Black-Scholes fallback
        return self._bs_spread_estimate(pos, symbol)

    def _bs_spread_estimate(self, pos: dict, symbol: str) -> Optional[float]:
        """
        Estimate spread premium via Black-Scholes using current spot and IV.

        Args:
            pos:    Open position dict (contains entry strikes via OCC symbols).
            symbol: Underlying ticker.

        Returns:
            Estimated premium per share, or None if data is unavailable.
        """
        try:
            spot   = self.broker.get_latest_price(symbol)
            if not spot:
                return None

            iv_data = self.iv_analyzer.get_iv_data(symbol)
            atm_iv  = iv_data.get("atm_iv", float(pos.get("entry_vrp", 0.20)) or 0.20)
            expiry  = pos.get("expiry", "")
            if not expiry:
                return None

            dte  = max(1, (date.fromisoformat(expiry) - date.today()).days)
            s_d  = float(pos.get("short_delta", 0.20))
            l_d  = float(pos.get("long_delta",  0.10))

            # Reconstruct approximate strikes from original deltas
            is_call = pos["strategy_type"] in (
                CREDIT_CALL_SPREAD, DEBIT_CALL_SPREAD, ZERO_DTE_CALL, IRON_CONDOR)
            opt_type = "call" if is_call else "put"

            short_g = GreeksEngine.compute_greeks(spot * (1 + s_d * 0.5), spot, dte,
                                                   atm_iv, opt_type)
            long_g  = GreeksEngine.compute_greeks(spot * (1 + l_d * 0.5), spot, dte,
                                                   atm_iv, opt_type)

            is_credit = pos["strategy_type"] in (
                CREDIT_PUT_SPREAD, CREDIT_CALL_SPREAD, IRON_CONDOR)
            if is_credit:
                return round(short_g["price"] - long_g["price"], 3)
            else:
                return round(long_g["price"] - short_g["price"], 3)

        except Exception as exc:
            log.debug("BS spread estimate failed %s: %s", symbol, exc)
            return None

    def _get_short_leg_delta_abs(
        self, pos: dict, symbol: str,
    ) -> Optional[float]:
        """
        Compute the current absolute delta of the short leg.

        Used for the delta emergency exit rule.

        Args:
            pos:    Open position dict.
            symbol: Underlying ticker.

        Returns:
            Absolute delta of the short leg (0.0–1.0), or None on failure.
        """
        try:
            spot   = self.broker.get_latest_price(symbol)
            if not spot:
                return None

            iv_data = self.iv_analyzer.get_iv_data(symbol)
            atm_iv  = iv_data.get("atm_iv", 0.20)
            expiry  = pos.get("expiry", "")
            dte     = max(1, (date.fromisoformat(expiry) - date.today()).days) if expiry else 1

            # Reconstruct short strike from stored short_delta at entry
            short_delta_entry = float(pos.get("short_delta", 0.20))
            is_call = pos["strategy_type"] in (
                CREDIT_CALL_SPREAD, DEBIT_CALL_SPREAD, ZERO_DTE_CALL)
            opt_type = "call" if is_call else "put"

            g = GreeksEngine.compute_greeks(spot, spot, dte, atm_iv, opt_type)
            return abs(g["delta"])

        except Exception as exc:
            log.debug("Short leg delta computation failed %s: %s", symbol, exc)
            return None

    # ── DTE helper ────────────────────────────────────────────────────────────

    @staticmethod
    def _compute_dte(expiry: str) -> int:
        """
        Return calendar days remaining to expiry.

        Args:
            expiry: Expiry date string (YYYY-MM-DD).

        Returns:
            Days remaining (≥ 0). Returns 0 for invalid or past dates.
        """
        if not expiry:
            return 0
        try:
            return max(0, (date.fromisoformat(expiry) - date.today()).days)
        except Exception:
            return 0

    # ── Portfolio snapshot ────────────────────────────────────────────────────

    def build_options_portfolio_snapshot(self) -> dict:
        """
        Compute aggregate portfolio-level stats across all open options positions.

        Used by the orchestrator for logging and pre-entry Greeks checks.

        Returns:
            Dict containing:
              open_count       – number of open positions
              total_pnl        – sum of current_pnl across all positions
              total_max_loss   – sum of max_loss (worst-case aggregate loss)
              portfolio_delta  – sum of net_delta
              portfolio_vega   – sum of net_vega
              portfolio_theta  – sum of net_theta (positive = favorable)
              daily_par        – total premium at risk
        """
        positions       = self.database.get_open_options_positions()
        total_pnl       = sum(float(p.get("current_pnl", 0))  for p in positions)
        total_max_loss  = sum(float(p.get("max_loss",    0))  for p in positions)
        portfolio_delta = sum(float(p.get("net_delta",   0))  for p in positions)
        portfolio_vega  = sum(float(p.get("net_vega",    0))  for p in positions)
        portfolio_theta = sum(float(p.get("net_theta",   0))  for p in positions)

        return {
            "open_count":      len(positions),
            "total_pnl":       round(total_pnl,       2),
            "total_max_loss":  round(total_max_loss,  2),
            "portfolio_delta": round(portfolio_delta, 2),
            "portfolio_vega":  round(portfolio_vega,  2),
            "portfolio_theta": round(portfolio_theta, 2),
            "daily_par":       round(total_max_loss,  2),
        }
