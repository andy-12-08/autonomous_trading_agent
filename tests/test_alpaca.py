"""Quick connectivity test — does not place any orders."""
from core.broker import AlpacaBroker

broker = AlpacaBroker()

acct = broker.get_account()
print(f"Account: equity={acct.equity} cash={acct.cash} status={acct.status}")

raw_settled = float(getattr(acct, "non_marginable_buying_power", None) or acct.cash or 0)
print(f"Settled cash: ${raw_settled:.2f}")

open_pos = broker.get_positions()
print(f"Open positions: {list(open_pos.keys()) or 'none'}")

clock = broker.is_market_open()
print(f"Market open: {clock}")

print("Alpaca connectivity OK")
