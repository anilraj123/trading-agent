"""Pack everything a sweep needs into ONE npz the farm nodes can load.

Same idea as sweep/cache.py: the fetch and the indicator precompute happen
once, centrally (Alpaca rate-limits per ACCOUNT, so three nodes fetching is
three times the quota for the same data), and only the part that varies per
config is distributed.

Emits research/_cache/farm_<tag>.npz -- numpy arrays only, loadable without
pandas.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import numpy as np
from research import data, engine, engine_np

CACHE = ROOT / "research" / "_cache"


def build(tag="pit", point_in_time=True):
    if point_in_time:
        from research import pit_data
        frames = pit_data.load()
    else:
        frames = data.load()
    _, sectors = data.universe_symbols()
    ind = engine.indicators(frames)
    A = engine_np.prepare(frames, ind, sectors)
    mask = None
    if point_in_time:
        from research import pit_data
        mask = pit_data.membership_mask(frames["close"].index, list(frames["close"].columns))
    out = {
        "open": A["open"], "high": A["high"], "low": A["low"], "close": A["close"],
        "sector": A["sector"], "n_sectors": np.array(A["n_sectors"]),
        "syms": A["syms"].astype("U12"),
        "dates": np.asarray(A["dates"]).astype("datetime64[D]").astype("int64"),
        "spy": frames["close"]["SPY"].to_numpy(np.float64),
    }
    for k, v in A["ind"].items():
        out["ind_" + k] = v
    if mask is not None:
        out["membership"] = mask
    path = CACHE / f"farm_{tag}.npz"
    np.savez_compressed(path, **out)
    print(f"wrote {path}  ({path.stat().st_size/1e6:.1f} MB)  "
          f"{A['close'].shape[0]} bars x {A['close'].shape[1]} symbols"
          f"{'  [point-in-time membership included]' if mask is not None else ''}")
    return path


if __name__ == "__main__":
    build(tag=sys.argv[1] if len(sys.argv) > 1 else "pit",
          point_in_time="--biased" not in sys.argv)
