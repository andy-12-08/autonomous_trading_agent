"""
Options executor mixin: routes ENTER decisions from OptionsDecisionEngine through
risk approval, strike selection, order submission, and persistence.

Execution flow for each ENTER decision:
  1. Validate inputs and fetch current spot price.
  2. Select optimal strikes for the strategy type using GreeksEngine.select_strike().
  3. Build OCC symbols using OptionsOrdersMixin.build_occ_symbol().
  4. Compute max loss and size position via OptionsRiskManager.
  5. Run OptionsRiskManager.approve_entry() — full gate check.
  6. Submit the order(s) via OptionsOrdersMixin (2-leg spread or 4-leg condor).
  7. Persist the position and record the decision.
  8. Send trade alert via Notifier.

Dependencies (injected by bootstrap.py):
  self.broker           – AlpacaBroker (also OptionsOrdersMixin)
  self.iv_analyzer      – IVAnalyzer
  self.options_risk     – OptionsRiskManager
  self.database         – Database
  self.notifier         – Notifier
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
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


class OptionsExecutorMixin:
    """
    Applies ENTER decisions to the market through risk gates, strike selection,
    and order submission.

    Expects these attributes on `self` (set by TradingOrchestrator):
      broker, iv_analyzer, options_risk, database, notifier,
      _daily_pnl, _open_positions_count, _ET, _dry_run
    """

    def execute_options_decisions(
        self,
        decisions:          list[dict],
        open_positions:     list[dict],
        daily_pnl:          float,
        total_equity:       float,
        consecutive_losses: int,
    ) -> None:
        """
        Dispatch each decision to the appropriate handler.

        Args:
            decisions:          Output of OptionsDecisionEngine.make_decisions().
            open_positions:     Open options positions from the database.
            daily_pnl:          Realized + unrealized P&L for the session.
            total_equity:       Account equity snapshot.
            consecutive_losses: Consecutive losing options trades today.

        Returns:
            None.
        """
        if self._dry_run:
            self._log_dry_run_options(decisions)
            return

        daily_par = OptionsRiskManager.compute_daily_premium_at_risk(open_positions)
        greeks_ok, greeks_msg = OptionsRiskManager.check_portfolio_greeks(open_positions)
        if not greeks_ok:
            log.warning("Portfolio Greeks breach detected: %s", greeks_msg)

        portfolio_delta = sum(float(p.get("net_delta", 0)) for p in open_positions)
        portfolio_vega  = sum(float(p.get("net_vega",  0)) for p in open_positions)
        open_count      = len([p for p in open_positions if p.get("status") == "open"])
        open_syms       = {p["symbol"] for p in open_positions}

        for d in decisions:
            action = d.get("action", "SKIP")
            symbol = d.get("symbol", "").upper()

            if action == "HOLD":
                continue

            if action == "SKIP":
                self.database.record_options_decision(
                    symbol=symbol, action="SKIP",
                    strategy_type=d.get("strategy_type"),
                    rationale=d.get("rationale", ""),
                    veto_rule="DECISION_ENGINE_SKIP",
                )
                continue

            if action == "ENTER":
                if symbol in open_syms:
                    log.debug("Skip ENTER %s — already in position", symbol)
                    continue
                self._handle_options_enter(
                    d              = d,
                    daily_par      = daily_par,
                    open_count     = open_count,
                    daily_pnl      = daily_pnl,
                    total_equity   = total_equity,
                    portfolio_delta = portfolio_delta,
                    portfolio_vega  = portfolio_vega,
                    consecutive_losses = consecutive_losses,
                )

    # ── Entry handler ─────────────────────────────────────────────────────────

    def _handle_options_enter(
        self,
        d:                  dict,
        daily_par:          float,
        open_count:         int,
        daily_pnl:          float,
        total_equity:       float,
        portfolio_delta:    float,
        portfolio_vega:     float,
        consecutive_losses: int,
    ) -> None:
        """
        Full entry workflow for a single ENTER decision.

        Args:
            d:                  ENTER decision dict from the decision engine.
            daily_par:          Current daily premium at risk.
            open_count:         Number of currently open options positions.
            daily_pnl:          Session P&L.
            total_equity:       Account equity.
            portfolio_delta:    Current portfolio delta.
            portfolio_vega:     Current portfolio vega.
            consecutive_losses: Consecutive losing trades this session.

        Returns:
            None.
        """
        symbol       = d["symbol"]
        strategy     = d["strategy_type"]
        direction    = d["direction"]
        target_dte   = d["target_dte"]
        spread_width = d["spread_width"]
        short_delta  = d["short_delta"]
        long_delta   = d["long_delta"]
        iv_rank      = d.get("iv_rank", 50.0)
        iv_regime    = d.get("iv_regime", "neutral")
        vrp          = d.get("vrp", 0.0)
        atm_iv       = d.get("atm_iv", 0.20)
        signal_score = d.get("signal_score", 0.0)
        market_regime = d.get("market_regime", "ranging")

        # ── Spot price ───────────────────────────────────────────────────────
        spot = d.get("spot_price") or self.broker.get_latest_price(symbol)
        if not spot:
            self._veto(symbol, strategy, "Could not fetch spot price", iv_rank,
                       iv_regime, vrp, atm_iv, signal_score, market_regime)
            return

        # ── Select expiry ────────────────────────────────────────────────────
        expiry = self._pick_expiry(symbol, target_dte)
        if not expiry:
            self._veto(symbol, strategy,
                       f"No qualifying expiry found near {target_dte} DTE",
                       iv_rank, iv_regime, vrp, atm_iv, signal_score, market_regime)
            return

        dte = (date.fromisoformat(expiry) - date.today()).days

        # ── Fetch option chain ───────────────────────────────────────────────
        chain = self.broker.get_option_chain(symbol, expiry)
        if chain is None:
            chain = self.iv_analyzer.get_chain_for_strategy(
                symbol, "call" if direction == "bullish" else "put",
                short_delta, target_dte)
        if chain is None:
            self._veto(symbol, strategy,
                       f"No options chain available for {symbol} {expiry}",
                       iv_rank, iv_regime, vrp, atm_iv, signal_score, market_regime)
            return

        # ── Route to strategy builder ────────────────────────────────────────
        result = self._build_strategy_legs(
            strategy, symbol, direction, spot, dte, expiry,
            spread_width, short_delta, long_delta, atm_iv, chain,
        )
        if result is None:
            self._veto(symbol, strategy,
                       "Could not select suitable strikes for the strategy",
                       iv_rank, iv_regime, vrp, atm_iv, signal_score, market_regime)
            return

        (long_sym, short_sym, long_delta_val, short_delta_val,
         entry_premium, max_profit, net_delta_pos, net_vega_pos,
         put_long_sym, put_short_sym, call_short_sym, call_long_sym) = result

        # ── Compute max loss ─────────────────────────────────────────────────
        is_iron_condor = (strategy == IRON_CONDOR)
        if is_iron_condor:
            max_loss_per_contract = OptionsRiskManager.compute_max_loss_iron_condor(
                spread_width, entry_premium, 1) / 100
        else:
            max_loss_per_contract = OptionsRiskManager.compute_max_loss_spread(
                spread_width, entry_premium, 1) / 100

        # ── Size the position ────────────────────────────────────────────────
        vix_level = self._get_vix_level()
        available_capital = self._get_available_capital(total_equity)
        contracts = OptionsRiskManager.size_position(
            strategy_type          = strategy,
            max_loss_per_contract  = max_loss_per_contract * 100,
            total_equity           = total_equity,
            daily_pnl              = daily_pnl,
            vix_level              = vix_level,
            available_capital      = available_capital,
        )
        if contracts <= 0:
            self._veto(symbol, strategy,
                       "Position sizing returned 0 contracts — capital exhausted",
                       iv_rank, iv_regime, vrp, atm_iv, signal_score, market_regime)
            return

        max_loss_dollars = OptionsRiskManager.compute_max_loss_spread(
            spread_width, entry_premium, contracts
        ) if not is_iron_condor else OptionsRiskManager.compute_max_loss_iron_condor(
            spread_width, entry_premium, contracts
        )

        # ── Risk approval ────────────────────────────────────────────────────
        ok, veto_reason = OptionsRiskManager.approve_entry(
            symbol                = symbol,
            strategy_type         = strategy,
            max_loss_dollars      = max_loss_dollars,
            daily_premium_at_risk = daily_par,
            open_positions_count  = open_count,
            daily_pnl             = daily_pnl,
            total_equity          = total_equity,
            portfolio_delta       = portfolio_delta,
            portfolio_vega        = portfolio_vega,
            new_position_delta    = abs(net_delta_pos * contracts * 100),
            new_position_vega     = abs(net_vega_pos  * contracts * 100),
            iv_rank               = iv_rank,
            vrp                   = vrp,
            signal_score          = signal_score,
            has_earnings_soon     = bool(d.get("has_earnings_soon", False)),
            consecutive_losses    = consecutive_losses,
        )
        if not ok:
            self._veto(symbol, strategy, veto_reason,
                       iv_rank, iv_regime, vrp, atm_iv, signal_score, market_regime,
                       veto_rule="RISK_MANAGER")
            return

        # ── Submit the order(s) ──────────────────────────────────────────────
        is_credit  = strategy in (CREDIT_PUT_SPREAD, CREDIT_CALL_SPREAD, IRON_CONDOR)
        order_result = self._submit_options_orders(
            strategy, symbol, contracts, entry_premium, is_iron_condor,
            long_sym, short_sym,
            put_long_sym, put_short_sym, call_short_sym, call_long_sym,
        )
        if order_result is None:
            self._veto(symbol, strategy, "Order submission failed",
                       iv_rank, iv_regime, vrp, atm_iv, signal_score, market_regime,
                       veto_rule="ORDER_FAILED")
            return

        long_order_id  = str(getattr(order_result.get("long_order"), "id", "") or "")
        short_order_id = str(getattr(order_result.get("short_order"), "id", "") or "")

        # ── Compute spread Greeks for portfolio tracking ──────────────────────
        is_call_side = direction == "bullish" or strategy in (DEBIT_CALL_SPREAD, ZERO_DTE_CALL)
        long_greeks  = GreeksEngine.compute_greeks(spot, 0, dte, atm_iv,
                                                   "call" if is_call_side else "put")
        short_greeks = GreeksEngine.compute_greeks(spot, 0, dte, atm_iv,
                                                   "call" if is_call_side else "put")
        spread_g = GreeksEngine.compute_spread_greeks(long_greeks, short_greeks, contracts)

        # ── Persist position ─────────────────────────────────────────────────
        position_id = str(uuid.uuid4())
        self.database.save_options_position(
            position_id       = position_id,
            symbol            = symbol,
            strategy_type     = strategy,
            contracts         = contracts,
            entry_premium     = entry_premium,
            max_profit        = max_profit * contracts * 100,
            max_loss          = max_loss_dollars,
            expiry            = expiry,
            target_dte        = dte,
            entry_iv_rank     = iv_rank,
            entry_vrp         = vrp,
            net_delta         = spread_g.get("net_delta", 0.0),
            net_theta         = spread_g.get("net_theta", 0.0),
            net_vega          = spread_g.get("net_vega",  0.0),
            short_delta       = short_delta_val,
            long_delta        = long_delta_val,
            long_symbol       = long_sym,
            short_symbol      = short_sym,
            put_long_symbol   = put_long_sym,
            put_short_symbol  = put_short_sym,
            call_short_symbol = call_short_sym,
            call_long_symbol  = call_long_sym,
            long_order_id     = long_order_id,
            short_order_id    = short_order_id,
        )

        # ── Record decision ──────────────────────────────────────────────────
        net_credit = entry_premium if is_credit else None
        net_debit  = entry_premium if not is_credit else None
        self.database.record_options_decision(
            symbol        = symbol,
            action        = "ENTER",
            strategy_type = strategy,
            position_id   = position_id,
            rationale     = d.get("rationale", ""),
            iv_rank       = iv_rank,
            iv_regime     = iv_regime,
            vrp           = vrp,
            atm_iv        = atm_iv,
            signal_score  = signal_score,
            market_regime = market_regime,
            net_credit    = net_credit,
            net_debit     = net_debit,
            contracts     = contracts,
            max_loss      = max_loss_dollars,
        )

        # ── Alert ────────────────────────────────────────────────────────────
        self.notifier.send_options_entry_alert(
            strategy_type = strategy,
            symbol        = symbol,
            contracts     = contracts,
            entry_premium = entry_premium,
            max_profit    = max_profit * contracts * 100,
            max_loss      = max_loss_dollars,
            expiry        = expiry,
            dte           = dte,
            iv_rank       = iv_rank,
            vrp           = vrp,
            rationale     = d.get("rationale", ""),
            long_symbol   = long_sym,
            short_symbol  = short_sym,
        )
        log.info("ENTER %s %s x%d | premium=%.2f max_loss=%.2f | %s",
                 symbol, strategy, contracts, entry_premium, max_loss_dollars,
                 d.get("rationale", "")[:60])

    # ── Strike selection and spread building ──────────────────────────────────

    def _build_strategy_legs(
        self,
        strategy:     str,
        symbol:       str,
        direction:    str,
        spot:         float,
        dte:          int,
        expiry:       str,
        spread_width: float,
        short_delta:  float,
        long_delta:   float,
        atm_iv:       float,
        chain,
    ) -> Optional[tuple]:
        """
        Select strikes and build OCC symbols for a given strategy type.

        Returns a tuple of 12 elements:
          (long_occ, short_occ, long_delta_val, short_delta_val,
           entry_premium, max_profit,
           net_delta_per_share, net_vega_per_share,
           put_long_occ, put_short_occ, call_short_occ, call_long_occ)

        Iron condor uses the put/call fields; single spreads use long/short only.
        Returns None if strikes cannot be found.
        """
        try:
            if strategy in (CREDIT_PUT_SPREAD, DEBIT_PUT_SPREAD, ZERO_DTE_PUT):
                return self._build_put_spread(
                    symbol, spot, dte, expiry, spread_width,
                    short_delta, long_delta, atm_iv, chain,
                    is_credit=(strategy == CREDIT_PUT_SPREAD),
                )

            if strategy in (CREDIT_CALL_SPREAD, DEBIT_CALL_SPREAD, ZERO_DTE_CALL):
                return self._build_call_spread(
                    symbol, spot, dte, expiry, spread_width,
                    short_delta, long_delta, atm_iv, chain,
                    is_credit=(strategy == CREDIT_CALL_SPREAD),
                )

            if strategy == IRON_CONDOR:
                return self._build_iron_condor(
                    symbol, spot, dte, expiry, spread_width,
                    short_delta, long_delta, atm_iv, chain,
                )

        except Exception as exc:
            log.warning("_build_strategy_legs failed %s %s: %s", symbol, strategy, exc)

        return None

    def _build_put_spread(self, symbol, spot, dte, expiry, spread_width,
                          short_delta, long_delta, atm_iv, chain, is_credit):
        """Select strikes for a put spread and return the leg tuple."""
        puts = chain.puts if hasattr(chain, "puts") else None
        if puts is None or puts.empty:
            return None

        # Short put leg (sells premium): target short_delta OTM
        short_row = GreeksEngine.select_strike(puts, short_delta, "put", spot, dte)
        if short_row is None:
            return None

        short_strike = short_row["strike"]
        long_strike  = round(short_strike - spread_width, 2)

        # Long put leg (protection): fixed width below short strike
        puts_below = puts[puts["strike"] <= short_strike - spread_width * 0.5]
        long_row   = GreeksEngine.select_strike(
            puts_below, long_delta, "put", spot, dte) if not puts_below.empty else None
        if long_row is None:
            # Fall back to fixed-width strike
            long_strike_fallback = short_strike - spread_width
            synthetic = {"strike": long_strike_fallback, "delta": -long_delta,
                         "price": short_row["price"] * 0.25, "bid": 0, "ask": 0,
                         "iv": atm_iv, "oi": 0, "volume": 0, "contract_sym": ""}
            long_row = synthetic

        long_strike = long_row["strike"] if isinstance(long_row, dict) else long_strike

        short_occ = self.broker.build_occ_symbol(symbol, expiry, "put", short_strike)
        long_occ  = self.broker.build_occ_symbol(symbol, expiry, "put", long_strike)

        short_price = short_row["price"]
        long_price  = long_row["price"] if isinstance(long_row, dict) else short_price * 0.3

        if is_credit:
            entry_premium = round(short_price - long_price, 2)
            max_profit    = entry_premium
        else:
            entry_premium = round(long_price - short_price, 2)
            max_profit    = round(spread_width - entry_premium, 2)

        # Greeks approximation for portfolio tracking
        short_g = GreeksEngine.compute_greeks(spot, short_strike, dte, atm_iv, "put")
        long_g  = GreeksEngine.compute_greeks(spot, long_strike,  dte, atm_iv, "put")
        spread_g = GreeksEngine.compute_spread_greeks(long_g, short_g)

        return (long_occ, short_occ,
                abs(long_g["delta"]), abs(short_g["delta"]),
                entry_premium, max_profit,
                spread_g["net_delta"] / 100, spread_g["net_vega"] / 100,
                None, None, None, None)

    def _build_call_spread(self, symbol, spot, dte, expiry, spread_width,
                           short_delta, long_delta, atm_iv, chain, is_credit):
        """Select strikes for a call spread and return the leg tuple."""
        calls = chain.calls if hasattr(chain, "calls") else None
        if calls is None or calls.empty:
            return None

        short_row = GreeksEngine.select_strike(calls, short_delta, "call", spot, dte)
        if short_row is None:
            return None

        short_strike = short_row["strike"]
        long_strike  = round(short_strike + spread_width, 2)

        calls_above = calls[calls["strike"] >= short_strike + spread_width * 0.5]
        long_row    = GreeksEngine.select_strike(
            calls_above, long_delta, "call", spot, dte) if not calls_above.empty else None
        if long_row is None:
            synthetic = {"strike": long_strike, "delta": long_delta,
                         "price": short_row["price"] * 0.25, "bid": 0, "ask": 0,
                         "iv": atm_iv, "oi": 0, "volume": 0, "contract_sym": ""}
            long_row  = synthetic

        long_strike = long_row["strike"] if isinstance(long_row, dict) else long_strike

        short_occ = self.broker.build_occ_symbol(symbol, expiry, "call", short_strike)
        long_occ  = self.broker.build_occ_symbol(symbol, expiry, "call", long_strike)

        short_price = short_row["price"]
        long_price  = long_row["price"] if isinstance(long_row, dict) else short_price * 0.3

        if is_credit:
            entry_premium = round(short_price - long_price, 2)
            max_profit    = entry_premium
        else:
            entry_premium = round(long_price - short_price, 2)
            max_profit    = round(spread_width - entry_premium, 2)

        short_g = GreeksEngine.compute_greeks(spot, short_strike, dte, atm_iv, "call")
        long_g  = GreeksEngine.compute_greeks(spot, long_strike,  dte, atm_iv, "call")
        spread_g = GreeksEngine.compute_spread_greeks(long_g, short_g)

        return (long_occ, short_occ,
                abs(long_g["delta"]), abs(short_g["delta"]),
                entry_premium, max_profit,
                spread_g["net_delta"] / 100, spread_g["net_vega"] / 100,
                None, None, None, None)

    def _build_iron_condor(self, symbol, spot, dte, expiry, spread_width,
                           short_delta, long_delta, atm_iv, chain):
        """Select strikes for all four legs of an iron condor."""
        puts  = chain.puts  if hasattr(chain, "puts")  else None
        calls = chain.calls if hasattr(chain, "calls") else None
        if puts is None or calls is None or puts.empty or calls.empty:
            return None

        # Short put
        put_short_row  = GreeksEngine.select_strike(puts,  short_delta, "put",  spot, dte)
        call_short_row = GreeksEngine.select_strike(calls, short_delta, "call", spot, dte)
        if put_short_row is None or call_short_row is None:
            return None

        put_short_strike  = put_short_row["strike"]
        call_short_strike = call_short_row["strike"]
        put_long_strike   = round(put_short_strike  - spread_width, 2)
        call_long_strike  = round(call_short_strike + spread_width, 2)

        put_long_occ    = self.broker.build_occ_symbol(symbol, expiry, "put",  put_long_strike)
        put_short_occ   = self.broker.build_occ_symbol(symbol, expiry, "put",  put_short_strike)
        call_short_occ  = self.broker.build_occ_symbol(symbol, expiry, "call", call_short_strike)
        call_long_occ   = self.broker.build_occ_symbol(symbol, expiry, "call", call_long_strike)

        put_short_g  = GreeksEngine.compute_greeks(spot, put_short_strike,  dte, atm_iv, "put")
        put_long_g   = GreeksEngine.compute_greeks(spot, put_long_strike,   dte, atm_iv, "put")
        call_short_g = GreeksEngine.compute_greeks(spot, call_short_strike, dte, atm_iv, "call")
        call_long_g  = GreeksEngine.compute_greeks(spot, call_long_strike,  dte, atm_iv, "call")

        put_credit  = round(put_short_row["price"]  * 0.8 - put_long_g["price"],   2)
        call_credit = round(call_short_row["price"] * 0.8 - call_long_g["price"],  2)
        net_credit  = round(put_credit + call_credit, 2)

        ic_g = GreeksEngine.compute_iron_condor_greeks(
            put_long_g, put_short_g, call_short_g, call_long_g)

        return (None, None,
                abs(put_long_g["delta"]), abs(put_short_g["delta"]),
                net_credit, net_credit,
                ic_g["net_delta"] / 100, ic_g["net_vega"] / 100,
                put_long_occ, put_short_occ, call_short_occ, call_long_occ)

    # ── Order submission router ───────────────────────────────────────────────

    def _submit_options_orders(
        self, strategy, symbol, contracts, entry_premium, is_iron_condor,
        long_sym, short_sym,
        put_long_sym, put_short_sym, call_short_sym, call_long_sym,
    ) -> Optional[dict]:
        """
        Submit the appropriate order type based on strategy.

        Returns the order result dict from the broker mixin, or None on failure.
        """
        if strategy == IRON_CONDOR:
            return self.broker.place_iron_condor(
                underlying         = symbol,
                put_long_symbol    = put_long_sym,
                put_short_symbol   = put_short_sym,
                call_short_symbol  = call_short_sym,
                call_long_symbol   = call_long_sym,
                contracts          = contracts,
                min_total_credit   = entry_premium,
            )

        if strategy in (CREDIT_PUT_SPREAD, CREDIT_CALL_SPREAD):
            return self.broker.place_credit_spread(
                underlying   = symbol,
                short_symbol = short_sym,
                long_symbol  = long_sym,
                contracts    = contracts,
                min_credit   = entry_premium,
            )

        if strategy in (DEBIT_CALL_SPREAD, DEBIT_PUT_SPREAD, ZERO_DTE_CALL, ZERO_DTE_PUT):
            return self.broker.place_debit_spread(
                underlying   = symbol,
                long_symbol  = long_sym,
                short_symbol = short_sym,
                contracts    = contracts,
                max_debit    = entry_premium,
            )

        log.warning("Unknown strategy type for order submission: %s", strategy)
        return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _pick_expiry(self, symbol: str, target_dte: int) -> Optional[str]:
        """
        Return the nearest available expiry within ±7 days of target_dte.

        Args:
            symbol:     Underlying ticker.
            target_dte: Desired days to expiry.

        Returns:
            Expiry date string (YYYY-MM-DD) or None if nothing qualifies.
        """
        try:
            expirations = self.iv_analyzer.get_available_expirations(symbol)
            today       = date.today()
            best        = None
            best_dist   = float("inf")

            for exp_str in expirations:
                dte = (date.fromisoformat(exp_str) - today).days
                if abs(dte - target_dte) < best_dist and dte >= 1:
                    best_dist = abs(dte - target_dte)
                    best      = exp_str

            return best if best_dist <= 14 else None
        except Exception as exc:
            log.warning("_pick_expiry failed %s: %s", symbol, exc)
            return None

    def _get_vix_level(self) -> float:
        """Fetch current VIX level from the broker or return a safe default."""
        try:
            return float(getattr(self, "_current_vix", 20.0) or 20.0)
        except Exception:
            return 20.0

    def _get_available_capital(self, total_equity: float) -> float:
        """Return the lesser of configured per-trade budget and available cash."""
        try:
            account = self.broker.get_account()
            cash    = float(getattr(account, "cash", total_equity * 0.20) or 0)
            return min(cash, config.MAX_PREMIUM_PER_TRADE * config.MAX_OPEN_OPTIONS_POSITIONS)
        except Exception:
            return config.MAX_PREMIUM_PER_TRADE * 3

    def _veto(self, symbol, strategy, reason, iv_rank, iv_regime, vrp, atm_iv,
              signal_score, market_regime, veto_rule: str = "ENTRY_VETO") -> None:
        """Log and record a vetoed options entry."""
        log.info("VETO %s %s: %s", symbol, strategy, reason)
        self.database.record_options_decision(
            symbol        = symbol,
            action        = "SKIP",
            strategy_type = strategy,
            rationale     = reason,
            iv_rank       = iv_rank,
            iv_regime     = iv_regime,
            vrp           = vrp,
            atm_iv        = atm_iv,
            signal_score  = signal_score,
            market_regime = market_regime,
            veto_rule     = veto_rule,
        )

    def _log_dry_run_options(self, decisions: list[dict]) -> None:
        """Log hypothetical options entries without submitting orders."""
        enters = [d for d in decisions if d.get("action") == "ENTER"]
        log.info("[DRY-RUN] %d ENTER decisions — no orders placed:", len(enters))
        for d in enters:
            log.info(
                "  ENTER %-6s %-22s | DTE=%-3d Δ=%.2f/%.2f | %s",
                d.get("symbol", "?"),
                d.get("strategy_type", "?"),
                d.get("target_dte", 0),
                d.get("short_delta", 0),
                d.get("long_delta",  0),
                d.get("rationale", "")[:70],
            )
