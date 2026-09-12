"""Do the new entry families fire at plausible rates, and on plausible names?
A signal that fires 0 or 900 times a day is misconfigured, and sweeping it
would just produce confident nonsense."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import numpy as np, pandas as pd
from research import data, engine, engine_np, pit_data

frames = pit_data.load(); ind = engine.indicators(frames)
_, sectors = data.universe_symbols()
A = engine_np.prepare(frames, ind, sectors)
mask = pit_data.membership_mask(frames["close"].index, list(frames["close"].columns))
dates = pd.DatetimeIndex(A["dates"])

for name, kw in [("meanrev rsi2<=10", dict(signal="meanrev", rsi2_max=10)),
                 ("meanrev rsi2<=5",  dict(signal="meanrev", rsi2_max=5)),
                 ("meanrev rsi2<=10, 3% below sma20",
                  dict(signal="meanrev", rsi2_max=10, dist_sma20_max=-3.0)),
                 ("gapfade <=-3%",    dict(signal="gapfade", gap_max_pct=-3.0)),
                 ("gapfade <=-5%",    dict(signal="gapfade", gap_max_pct=-5.0))]:
    q = engine_np.qualified_np(A, engine.Params(**kw)) & mask
    per_day = q[250:].sum(1)
    print(f"  {name:34} fires/day  mean {per_day.mean():6.1f}  median {np.median(per_day):5.0f}  "
          f"max {per_day.max():4d}  zero-days {100*(per_day==0).mean():4.1f}%")

# spot-check gapfade on a day with a known market-wide gap down
q = engine_np.qualified_np(A, engine.Params(signal="gapfade", gap_max_pct=-3.0)) & mask
i = int(np.argmax(q[250:].sum(1))) + 250
print(f"\n  busiest gapfade day: {dates[i]:%Y-%m-%d} with {q[i].sum()} names "
      f"(market-wide gap days should dominate)")
names = A["syms"][np.flatnonzero(q[i])][:8]
print("   e.g.", ", ".join(names))
