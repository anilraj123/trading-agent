"""Bars + membership mask for the point-in-time universe.

`ever_members()` is the union of every symbol that was in the S&P 500 or 400
at ANY month-end -- survivors and casualties alike. That union is what we
fetch bars for, and it is materially larger than today's 903.

`membership_mask()` turns the monthly snapshots into a daily bool
[date x symbol] grid, forward-filled between snapshots. The backtest uses it
to gate ENTRIES only: a name must have been an index member on the signal bar
to be bought. Positions already open are not force-sold when a name leaves the
index -- the ordinary exit rules still govern them, which is what a real
portfolio does.

Ticker reuse is real (BBBY trades again after the bankruptcy), so membership
is always joined on (date, symbol), never symbol alone.
"""
import json, sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from research import data as _data

CACHE = ROOT / "research" / "_cache"
PIT = CACHE / "pit_membership.json"
PIT_BARS = CACHE / "bars_pit.parquet"


def snapshots():
    d = json.loads(PIT.read_text())
    out = {}
    for idx, snaps in d.items():
        for ds, syms in snaps.items():
            out.setdefault(ds, set()).update(syms)
    return dict(sorted(out.items()))


def ever_members():
    s = set()
    for syms in snapshots().values():
        s |= syms
    return sorted(s)


def build_bars(extra=("SPY",)):
    syms = sorted(set(ever_members()) | set(extra))
    print(f"fetching {len(syms)} ever-members (vs {len(_data.universe_symbols()[0])} current)")
    df = _data.fetch(syms)
    df.to_parquet(PIT_BARS)
    idx = df.index.get_level_values(1)
    print(f"wrote {PIT_BARS} ({PIT_BARS.stat().st_size/1e6:.1f} MB)  rows {len(df):,}  "
          f"symbols {df.index.get_level_values(0).nunique()}  "
          f"range {idx.min():%Y-%m-%d} -> {idx.max():%Y-%m-%d}")


def load():
    df = pd.read_parquet(PIT_BARS).reset_index()
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None).dt.normalize()
    return {f: df.pivot_table(index="timestamp", columns="symbol", values=f).sort_index()
            for f in ("open", "high", "low", "close", "volume")}


def membership_mask(dates, symbols):
    """bool [len(dates) x len(symbols)], forward-filled from month-end snaps."""
    snaps = snapshots()
    snap_dates = pd.to_datetime(list(snaps))
    col = {s: i for i, s in enumerate(symbols)}
    grid = np.zeros((len(snap_dates), len(symbols)), bool)
    for r, ds in enumerate(snaps):
        for s in snaps[ds]:
            j = col.get(s)
            if j is not None:
                grid[r, j] = True
    frame = pd.DataFrame(grid, index=snap_dates, columns=symbols)
    # a snapshot dated month-end governs the days AFTER it
    out = frame.reindex(frame.index.union(pd.DatetimeIndex(dates))).ffill()
    return out.reindex(pd.DatetimeIndex(dates)).fillna(False).to_numpy(bool)


if __name__ == "__main__":
    if not PIT.exists():
        sys.exit("pit_membership.json missing - run research/pit_universe.py first")
    snaps = snapshots()
    ev = ever_members()
    print(f"{len(snaps)} month-end snapshots {min(snaps)} -> {max(snaps)}")
    print(f"ever-members: {len(ev)}   current universe: {len(_data.universe_symbols()[0])}")
    cur = set(_data.universe_symbols()[0])
    print(f"in history but NOT in today's index (the survivorship gap): {len(set(ev) - cur)}")
    if "--fetch" in sys.argv:
        build_bars()
