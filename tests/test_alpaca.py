"""Quick connectivity test — does not place any orders."""
import alpaca_client as broker

acct = broker.get_account()
print(f"Account: equity={acct.equity} cash={acct.cash} status={acct.status}")

cash = broker.get_settled_cash()
print(f"Settled cash: ${cash:.2f}")

open_pos = broker.get_positions()
print(f"Open positions: {list(open_pos.keys()) or 'none'}")

clock = broker.is_market_open()
print(f"Market open: {clock}")

print("Alpaca connectivity OK")
