from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, StopOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from core.database import log


class OrdersMixin:
    """Order placement helpers mixed into AlpacaBroker."""

    def cancel_all_orders(self) -> None:
        """Cancel every open order on the account.

        Returns:
            None.
        """
        self._trade_client.cancel_orders()

    def cancel_orders_for_symbol(self, symbol: str) -> None:
        """Cancel all open orders for a single symbol (e.g. bracket legs before a manual exit).

        Args:
            symbol: Ticker whose open orders should be cancelled.

        Returns:
            None.
        """
        orders = self.get_open_orders()
        for o in orders:
            if o.symbol == symbol:
                try:
                    self._trade_client.cancel_order_by_id(str(o.id))
                    log.info("Cancelled order %s for %s", o.id, symbol)
                except Exception as e:
                    log.warning("Could not cancel order %s for %s: %s", o.id, symbol, e)

    def close_position(self, symbol: str) -> bool:
        """Flatten one position via the broker API.

        Args:
            symbol: Ticker to close.

        Returns:
            True when the broker accepts the close; False on any error.
        """
        try:
            self._trade_client.close_position(symbol)
            log.info("Closed position: %s", symbol)
            return True
        except Exception as e:
            log.error("Failed to close %s: %s", symbol, e)
            return False

    def place_market_order(self, symbol: str, qty: float, side: str) -> object:
        """Submit a day time-in-force market order.

        Args:
            symbol: Equity ticker.
            qty: Share quantity (may be fractional depending on account).
            side: BUY or SELL string.

        Returns:
            Alpaca order object on success, or None when submission fails.
        """
        order_side = OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=order_side,
            time_in_force=TimeInForce.DAY,
        )
        try:
            order = self._trade_client.submit_order(req)
            log.info("Market %s %s x%.2f submitted | id=%s", side, symbol, qty, order.id)
            return order
        except Exception as e:
            log.error("Market order failed %s %s: %s", side, symbol, e)
            return None

    def place_bracket_order(self, symbol: str, qty: float, stop_loss: float,
                            take_profit: float,
                            limit_price: float | None = None) -> object:
        """Submit a bracket BUY with protective stop and take-profit legs.

        Args:
            symbol: Equity ticker.
            qty: Share quantity for the parent order.
            stop_loss: Stop price for the protective sell leg.
            take_profit: Limit price for the take-profit sell leg.
            limit_price: When set, use a limit entry; otherwise use a market entry.

        Returns:
            Alpaca order object on success, or None on failure.
        """
        sl_leg = {"stop_price":  round(stop_loss,   2)}
        tp_leg = {"limit_price": round(take_profit,  2)}

        if limit_price is not None:
            req = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
                order_class="bracket",
                stop_loss=sl_leg,
                take_profit=tp_leg,
            )
            order_label = "Limit"
        else:
            req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                order_class="bracket",
                stop_loss=sl_leg,
                take_profit=tp_leg,
            )
            order_label = "Market"

        try:
            order = self._trade_client.submit_order(req)
            log.info("%s Bracket BUY %s x%.2f | entry=%.2f SL=%.2f TP=%.2f | id=%s",
                     order_label, symbol, qty,
                     limit_price or 0, stop_loss, take_profit, order.id)
            return order
        except Exception as e:
            log.error("Bracket order failed %s: %s", symbol, e)
            return None

    def update_stop_loss(self, symbol: str, new_stop: float):
        """Cancel existing stop orders for the symbol, then submit a stop-market sell for full qty.

        Args:
            symbol: Open position ticker.
            new_stop: Desired stop trigger price.

        Returns:
            Alpaca order object on success, or None when skipped or failed.
        """
        _STOP_TYPES = {"stop", "stop_limit", "trailing_stop"}

        def _cancel_if_stop(o) -> bool:
            o_type = str(getattr(o, "order_type", "") or getattr(o, "type", "") or "").lower()
            o_side = str(getattr(o, "side", "")).lower()
            o_sym  = str(getattr(o, "symbol", "")).upper()
            if o_sym != symbol.upper() or "sell" not in o_side or o_type not in _STOP_TYPES:
                return False
            try:
                self._trade_client.cancel_order_by_id(str(o.id))
                log.info("Cancelled stop leg %s (%s) for %s before stop update", o.id, o_type, symbol)
            except Exception:
                pass
            return True

        orders = self.get_open_orders()
        for o in orders:
            _cancel_if_stop(o)
            for leg in (getattr(o, "legs", None) or []):
                _cancel_if_stop(leg)
        positions = self.get_positions()
        if symbol not in positions:
            return
        qty = float(positions[symbol].qty)
        req = StopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            stop_price=round(new_stop, 2),
        )
        try:
            order = self._trade_client.submit_order(req)
            log.info("New stop for %s @ %.2f | id=%s", symbol, new_stop, order.id)
            return order
        except Exception as e:
            err = str(e)
            if "insufficient qty" in err or "40310000" in err:
                log.info("Stop resubmit skipped for %s — bracket order already protecting position", symbol)
            else:
                log.error("Stop update failed %s: %s", symbol, e)
            return None

