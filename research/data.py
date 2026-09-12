"""Daily-bar cache for the backtests.

One parquet per run of `python -m research.data`; symbols are fetched in
chunks (Alpaca takes multi-symbol requests) and cached so every sweep reads
from disk, not the API.

SURVIVORSHIP BIAS WARNING: research/universe.json is TODAY's S&P 500+400
membership. Backtesting it over history silently excludes every company that
was dropped from the index (bankruptcies, takeunders, chronic
underperformers), which flatters results. Treat absolute returns from this
universe as an UPPER BOUND, and prefer conclusions that compare strategies
against each other on the same biased sample.
"""
import json, os, sys, time
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

CACHE = ROOT / "research" / "_cache"
BARS = CACHE / "bars_daily.parquet"
START = datetime(2016, 1, 1)
CHUNK = 200


def universe_symbols():
    d = json.loads((ROOT / "research" / "universe.json").read_text())
    return sorted(d["members"]), d["members"]


def _client():
    return StockHistoricalDataClient(os.getenv("ALPACA_API_KEY"),
                                     os.getenv("ALPACA_SECRET_KEY"))


def fetch(symbols, start=START, chunk=CHUNK):
    c, frames = _client(), []
    for i in range(0, len(symbols), chunk):
        part = symbols[i:i + chunk]
        t0 = time.time()
        df = c.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=part, timeframe=TimeFrame.Day, start=start)).df
        frames.append(df)
        got = df.index.get_level_values(0).nunique()
        print(f"  [{i + len(part):4d}/{len(symbols)}] {time.time() - t0:6.1f}s "
              f"rows={len(df):8d} symbols={got}/{len(part)}", flush=True)
    return pd.concat(frames)


def build(extra=("SPY",)):
    syms, _ = universe_symbols()
    syms = sorted(set(syms) | set(extra))
    print(f"fetching {len(syms)} symbols from {START:%Y-%m-%d}")
    df = fetch(syms)
    CACHE.mkdir(parents=True, exist_ok=True)
    df.to_parquet(BARS)
    idx = df.index.get_level_values(1)
    print(f"\nwrote {BARS}  ({BARS.stat().st_size/1e6:.1f} MB)")
    print(f"  rows {len(df):,}  symbols {df.index.get_level_values(0).nunique()}")
    print(f"  range {idx.min():%Y-%m-%d} -> {idx.max():%Y-%m-%d}")


def load():
    """Wide frames keyed by field: {'close': DataFrame[date x symbol], ...}"""
    df = pd.read_parquet(BARS)
    df = df.reset_index()
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None).dt.normalize()
    out = {}
    for f in ("open", "high", "low", "close", "volume"):
        out[f] = df.pivot_table(index="timestamp", columns="symbol", values=f).sort_index()
    return out


if __name__ == "__main__":
    build()
