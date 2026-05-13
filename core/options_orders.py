"""
Options order execution mixin for AlpacaBroker.

Handles single-leg options orders, vertical spreads (debit and credit),
iron condors, and position close orders.  Uses the Alpaca v2 options API.

NOTE: Alpaca paper trading supports options in limited form.  Full options
      trading requires a live account with the options trading agreement
      signed.  The order schema used here matches Alpaca SDK v0.30+.

OCC symbol format: AAPL241220C00185000
  AAPL     – underlying symbol (up to 6 chars, padded)
  241220   – expiry YYMMDD
  C        – C=call, P=put
  00185000 – strike × 1000, zero-padded to 8 digits
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from core.database import log


class OptionsOrdersMixin:
    """
    Options order helpers mixed into AlpacaBroker.

    All monetary values are per-share (multiply by 100 for per-contract).
    Quantities are in contracts.
    """

    # ── Symbol construction ───────────────────────────────────────────────────

    @staticmethod
    def build_occ_symbol(
        underlying: str,
        expiry:     str,
        option_type: str,
        strike:     float,
    ) -> str:
        """
        Construct an OCC-format option symbol from its components.

        Args:
            underlying:  Ticker symbol (e.g. 'AAPL').
            expiry:      Expiry date as 'YYYY-MM-DD'.
            option_type: 'call' or 'put'.
            strike:      Strike price in dollars (e.g. 185.0).

        Returns:
            OCC symbol string (e.g. 'AAPL241220C00185000').
        """
        exp_date    = date.fromisoformat(expiry)
        exp_str     = exp_date.strftime("%y%m%d")
        cp          = "C" if option_type.lower() == "call" else "P"
        strike_int  = int(round(strike * 1000))
        strike_str  = f"{strike_int:08d}"
        underlying  = underlying.upper().ljust(6)[:6].rstrip()   # OCC pads to 6 chars
        return f"{underlying}{exp_str}{cp}{strike_str}"

    # ── Single-leg orders ─────────────────────────────────────────────────────

    def place_single_leg_option(
        self,
        option_symbol: str,
        contracts:     int,
        side:          str,
        limit_price:   float,
    ) -> Optional[object]:
        """
        Submit a single-leg options limit order.

        Args:
            option_symbol: OCC-format option symbol (e.g. 'AAPL241220C00185000').
            contracts:     Number of contracts.
            side:          'buy' or 'sell'.
            limit_price:   Limit price per share (per-contract = limit_price × 100).

        Returns:
            Alpaca order object on success, or None on failure.
        """
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        req = LimitOrderRequest(
            symbol          = option_symbol,
            qty             = contracts,
            side            = order_side,
            time_in_force   = TimeInForce.DAY,
            limit_price     = round(limit_price, 2),
            asset_class     = AssetClass.US_OPTION,
        )
        try:
            order = self._trade_client.submit_order(req)
            log.info("Single-leg %s %s x%d @ %.2f | id=%s",
                     side.upper(), option_symbol, contracts, limit_price, order.id)
            return order
        except Exception as exc:
            log.error("Single-leg option order failed %s %s: %s", side, option_symbol, exc)
            return None

    # ── Spread orders ─────────────────────────────────────────────────────────

    def place_debit_spread(
        self,
        underlying:       str,
        long_symbol:      str,
        short_symbol:     str,
        contracts:        int,
        max_debit:        float,
    ) -> Optional[dict]:
        """
        Submit a debit spread as two separate limit orders.

        A debit spread buys the ITM/ATM leg and sells an OTM leg to reduce
        cost and vega exposure.  We use two separate orders because Alpaca's
        multi-leg spread API requires a live account.

        Args:
            underlying:  Underlying ticker (for logging).
            long_symbol:  OCC symbol for the option we BUY.
            short_symbol: OCC symbol for the option we SELL.
            contracts:    Number of contracts for each leg.
            max_debit:    Maximum net debit per share willing to pay.

        Returns:
            Dict with long_order and short_order on success, or None on failure.
        """
        long_price  = round(max_debit * 0.60, 2)   # buy up to 60% of max debit on long
        short_price = round(max_debit * 0.20, 2)   # sell short at minimum 20% offset

        long_order = self.place_single_leg_option(
            long_symbol, contracts, "buy", long_price
        )
        if not long_order:
            log.error("Debit spread FAILED %s — long leg rejected", underlying)
            return None

        short_order = self.place_single_leg_option(
            short_symbol, contracts, "sell", short_price
        )
        if not short_order:
            log.warning("Debit spread PARTIAL %s — short leg failed, cancelling long", underlying)
            try:
                self._trade_client.cancel_order_by_id(str(long_order.id))
            except Exception:
                pass
            return None

        log.info("Debit spread placed %s: long=%s short=%s x%d | debit≈%.2f",
                 underlying, long_symbol, short_symbol, contracts, long_price - short_price)
        return {"long_order": long_order, "short_order": short_order, "net_debit": long_price - short_price}

    def place_credit_spread(
        self,
        underlying:    str,
        short_symbol:  str,
        long_symbol:   str,
        contracts:     int,
        min_credit:    float,
    ) -> Optional[dict]:
        """
        Submit a credit spread as two separate limit orders.

        A credit spread sells the closer-to-ATM leg and buys a further OTM
        wing for protection.  The net credit is our maximum profit.

        Args:
            underlying:    Underlying ticker (for logging).
            short_symbol:  OCC symbol for the option we SELL (premium collection leg).
            long_symbol:   OCC symbol for the option we BUY (protective wing).
            contracts:     Number of contracts for each leg.
            min_credit:    Minimum net credit per share required for entry.

        Returns:
            Dict with short_order and long_order on success, or None on failure.
        """
        short_price = round(min_credit * 1.20, 2)   # sell at ask-ish
        long_price  = round(min_credit * 0.25, 2)   # buy wing cheaply

        short_order = self.place_single_leg_option(
            short_symbol, contracts, "sell", short_price
        )
        if not short_order:
            log.error("Credit spread FAILED %s — short leg rejected", underlying)
            return None

        long_order = self.place_single_leg_option(
            long_symbol, contracts, "buy", long_price
        )
        if not long_order:
            log.warning("Credit spread PARTIAL %s — long leg failed, cancelling short", underlying)
            try:
                self._trade_client.cancel_order_by_id(str(short_order.id))
            except Exception:
                pass
            return None

        net_credit = short_price - long_price
        log.info("Credit spread placed %s: short=%s long=%s x%d | credit≈%.2f",
                 underlying, short_symbol, long_symbol, contracts, net_credit)
        return {"short_order": short_order, "long_order": long_order, "net_credit": net_credit}

    def place_iron_condor(
        self,
        underlying:         str,
        put_long_symbol:    str,
        put_short_symbol:   str,
        call_short_symbol:  str,
        call_long_symbol:   str,
        contracts:          int,
        min_total_credit:   float,
    ) -> Optional[dict]:
        """
        Submit a 4-leg iron condor as two credit spreads.

        An iron condor = short put spread + short call spread.
        Both wings are entered simultaneously; if either leg fails,
        the existing legs are cancelled to avoid a naked short.

        Args:
            underlying:        Underlying ticker (for logging).
            put_long_symbol:   OCC symbol for long put (lower strike, protection).
            put_short_symbol:  OCC symbol for short put (premium collection).
            call_short_symbol: OCC symbol for short call (premium collection).
            call_long_symbol:  OCC symbol for long call (upper strike, protection).
            contracts:         Number of condors.
            min_total_credit:  Minimum combined credit per share to proceed.

        Returns:
            Dict with all four leg orders and net_credit on full success,
            or None if any leg fails (all legs cancelled on failure).
        """
        half_credit = min_total_credit / 2.0

        put_spread = self.place_credit_spread(
            underlying, put_short_symbol, put_long_symbol, contracts, half_credit
        )
        if not put_spread:
            return None

        call_spread = self.place_credit_spread(
            underlying, call_short_symbol, call_long_symbol, contracts, half_credit
        )
        if not call_spread:
            log.warning("Iron condor PARTIAL %s — call spread failed, unwinding put spread", underlying)
            for order in (put_spread.get("short_order"), put_spread.get("long_order")):
                if order:
                    try:
                        self._trade_client.cancel_order_by_id(str(order.id))
                    except Exception:
                        pass
            return None

        net_credit = put_spread["net_credit"] + call_spread["net_credit"]
        log.info("Iron condor placed %s: x%d | net credit≈%.2f", underlying, contracts, net_credit)
        return {
            "put_short_order":  put_spread["short_order"],
            "put_long_order":   put_spread["long_order"],
            "call_short_order": call_spread["short_order"],
            "call_long_order":  call_spread["long_order"],
            "net_credit":       net_credit,
        }

    # ── Close helpers ─────────────────────────────────────────────────────────

    def close_option_leg(
        self,
        option_symbol: str,
        contracts:     int,
        side:          str,
        limit_price:   Optional[float] = None,
    ) -> Optional[object]:
        """
        Close a single options leg with a market or limit order.

        For buys-to-close (selling what we own): use limit near the bid.
        For sells-to-close (buying back what we sold): use limit near the ask.

        Args:
            option_symbol: OCC-format symbol.
            contracts:     Number of contracts to close.
            side:          'buy' (closing a short) or 'sell' (closing a long).
            limit_price:   Limit price per share; if None uses a market order.

        Returns:
            Alpaca order object on success, or None on failure.
        """
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL

        if limit_price is not None:
            req = LimitOrderRequest(
                symbol        = option_symbol,
                qty           = contracts,
                side          = order_side,
                time_in_force = TimeInForce.DAY,
                limit_price   = round(limit_price, 2),
                asset_class   = AssetClass.US_OPTION,
            )
        else:
            req = MarketOrderRequest(
                symbol        = option_symbol,
                qty           = contracts,
                side          = order_side,
                time_in_force = TimeInForce.DAY,
                asset_class   = AssetClass.US_OPTION,
            )

        try:
            order = self._trade_client.submit_order(req)
            log.info("Close option leg: %s %s x%d @ %s | id=%s",
                     side.upper(), option_symbol, contracts,
                     f"{limit_price:.2f}" if limit_price else "MARKET", order.id)
            return order
        except Exception as exc:
            log.error("Close option leg failed %s: %s", option_symbol, exc)
            return None

    def close_spread_position(
        self,
        long_symbol:  str,
        short_symbol: str,
        contracts:    int,
        long_limit:   Optional[float] = None,
        short_limit:  Optional[float] = None,
    ) -> bool:
        """
        Close a two-leg spread position by closing each leg.

        Args:
            long_symbol:  OCC symbol for the leg we own (sell to close).
            short_symbol: OCC symbol for the leg we are short (buy to close).
            contracts:    Number of contracts.
            long_limit:   Optional limit price for selling the long leg.
            short_limit:  Optional limit price for buying back the short leg.

        Returns:
            True if both legs were submitted successfully, False if either failed.
        """
        sell_long = self.close_option_leg(long_symbol, contracts, "sell", long_limit)
        if not sell_long:
            log.error("Spread close FAILED — could not sell long leg %s", long_symbol)
            return False

        buy_short = self.close_option_leg(short_symbol, contracts, "buy", short_limit)
        if not buy_short:
            log.warning("Spread close PARTIAL — short leg buyback failed for %s", short_symbol)
            return False

        log.info("Spread closed: sold %s, bought back %s", long_symbol, short_symbol)
        return True
