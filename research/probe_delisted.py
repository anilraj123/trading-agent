"""Can we get bars for companies that no longer trade? If not, a point-in-time
universe is impossible with this data source and survivorship bias is
unfixable here."""
import os, sys
from datetime import datetime
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

c = StockHistoricalDataClient(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))
# known removals: acquired, failed, or delisted after 2016
TESTS = {
    "SIVB": "Silicon Valley Bank - failed Mar 2023",
    "FRC":  "First Republic - failed May 2023",
    "TWTR": "Twitter - taken private Oct 2022",
    "ATVI": "Activision - acquired Oct 2023",
    "CERN": "Cerner - acquired Jun 2022",
    "XLNX": "Xilinx - acquired Feb 2022",
    "BBBY": "Bed Bath & Beyond - bankrupt 2023",
    "RE":   "Everest Re - renamed 2023",
    "AAPL": "control (still listed)",
}
for sym, note in TESTS.items():
    try:
        df = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=[sym],
                timeframe=TimeFrame.Day, start=datetime(2016, 1, 1))).df
        if len(df) == 0:
            print(f"  {sym:6} NO DATA                         {note}")
        else:
            idx = df.index.get_level_values(1)
            print(f"  {sym:6} {len(df):5d} bars  {idx.min():%Y-%m-%d} -> {idx.max():%Y-%m-%d}   {note}")
    except Exception as e:
        print(f"  {sym:6} ERROR {type(e).__name__}: {str(e)[:60]}   {note}")
