from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, StopOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from core.database import log


class OrdersMixin:
    def cancel_all_orders(self) -> None:
        """Cancel every open order on the account.

        Returns:
            None.
        """
        self._trade_client.cancel_orders()

    def close_position(self, symbol: str) -> None:
        """Args:
            symbol: Position symbol to flatten.

        Returns:
            None.
        """
        try:
            self._trade_client.close_position(symbol)
            log.info("Closed position: %s", symbol)
        except Exception as e:
            log.error("Failed to close %s: %s", symbol, e)

    def place_market_order(self, symbol: str, qty: float, side: str) -> object:
        """Submit a day market order.

        Args:
            symbol: Ticker.
            qty: Shares.
            side: BUY or SELL.

        Returns:
            Alpaca order object, or None on failure.
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
        """Submit a bracket BUY (market or limit entry) with SL/TP legs.

        Args:
            symbol: Ticker.
            qty: Shares.
            stop_loss: Protective stop price.
            take_profit: Limit take-profit price.
            limit_price: If set, limit entry; else market entry.

        Returns:
            Alpaca order object, or None on failure.
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
        """Cancel prior stop if present and submit a new stop-market sell.

        Args:
            symbol: Position symbol.
            new_stop: New stop price.

        Returns:
            Alpaca order object, or None if skipped/failed.
        """
        orders = self.get_open_orders()
        for o in orders:
            if o.symbol == symbol and o.order_type in ("stop", "stop_limit"):
                try:
                    self._trade_client.cancel_order_by_id(str(o.id))
                    log.info("Cancelled old stop for %s", symbol)
                except Exception:
                    pass
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

