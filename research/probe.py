import os, time
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from datetime import datetime
c = StockHistoricalDataClient(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))
for start in (datetime(2010,1,1), datetime(2016,1,1)):
    t0 = time.time()
    try:
        df = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=["AAPL","SPY"],
                timeframe=TimeFrame.Day, start=start)).df
        idx = df.index.get_level_values(1)
        print(f"start={start:%Y-%m-%d}  {time.time()-t0:5.1f}s  rows={len(df):6d}  "
              f"actual range {idx.min():%Y-%m-%d} -> {idx.max():%Y-%m-%d}")
    except Exception as e:
        print(f"start={start:%Y-%m-%d}  FAILED: {type(e).__name__}: {str(e)[:200]}")
