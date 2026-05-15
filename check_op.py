from datetime import date, timedelta
from trader.config import Config
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest
from alpaca.data import OptionHistoricalDataClient
from alpaca.data.requests import OptionSnapshotRequest

data = OptionHistoricalDataClient(Config.ALPACA_API_KEY, Config.ALPACA_SECRET_KEY)
client = TradingClient(Config.ALPACA_API_KEY, Config.ALPACA_SECRET_KEY, paper="paper-api" in Config.ALPACA_BASE_URL)

for sym in ["SPY", "AAPL", "QQQ"]:
    req = GetOptionContractsRequest(
        underlying_symbols=[sym], status="active",
        expiration_date_gte=date.today() + timedelta(days=7),
        expiration_date_lte=date.today() + timedelta(days=21)
    )
    contracts = client.get_option_contracts(req)
    pool = contracts.option_contracts
    if not pool:
        print(f"{sym}: no contracts"); continue
    price = float(max(pool, key=lambda c: float(c.strike_price)).strike_price)  # rough
    print(f"\n{sym}:")
    for c in pool:
        try:
            s = float(c.strike_price)
            p = float(c.strike_price)
            if p < 1: continue
        except: continue
    calls = [c for c in pool if c.type == "call" and float(c.strike_price) > 700 if sym == "SPY" or True]
    near_calls = sorted([c for c in calls if "SPY" not in sym or float(c.strike_price) >= 735], key=lambda x: float(x.strike_price))[:3]
    near_puts = sorted([c for c in pool if c.type == "put" and ("SPY" not in sym or float(c.strike_price) <= 745)], key=lambda x: -float(x.strike_price))[:3]
    for label, items in [("CALL", near_calls), ("PUT", near_puts)]:
        for c in items:
            try:
                snap = data.get_option_snapshot(OptionSnapshotRequest(symbol_or_symbols=c.symbol))
                q = snap.latest_quote
                if q and q.bid and q.ask:
                    mid = (q.bid + q.ask) / 2
                    dte = (c.expiration_date - date.today()).days
                    print(f"  {label} ${c.strike_price:.0f} {dte}dte: bid=${q.bid:.2f} ask=${q.ask:.2f} mid=${mid:.2f}")
            except: pass
