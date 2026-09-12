"""Walk-forward validation. Pick on TRAIN, score on unseen TEST.

TTL is capped at 120d deliberately: beyond that the simulator increasingly
just holds today's S&P constituents from 2016, and the result measures
survivorship bias rather than a strategy.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pandas as pd
from research import sweep

SPACE = {
    "ttl_days":           [10, 20, 40, 80, 120],
    "disaster_stop_pct":  [-0.05, -0.08, -0.15],
    "invalidation_pct":   [-0.05, -0.999],
    "trail_activate_pct": [0.05, 0.10, 0.25, 9.99],
    "trail_stop_pct":     [-0.03, -0.10],
    "cost_bps":           [10.0],
}
FOLDS = [
    ("2016-04-01", "2019-12-31", "2020-01-01", "2021-12-31"),
    ("2018-01-01", "2021-12-31", "2022-01-01", "2023-12-31"),
    ("2020-01-01", "2023-12-31", "2024-01-01", "2025-12-31"),
    ("2022-01-01", "2025-12-31", "2026-01-01", "2026-09-11"),
]
combos = sweep.grid(SPACE)
print(f"{len(combos)} configs x {len(FOLDS)} folds")
for obj in ("sharpe", "total_return_pct"):
    print(f"\n{'='*78}\nOBJECTIVE: {obj}\n{'='*78}")
    res, _ = sweep.walk_forward(combos, FOLDS, objective=obj, procs=18)
    if res.empty:
        continue
    cols = ["fold", "_start", "_end", "total_return_pct", "bench_return_pct",
            "alpha_pct", "sharpe", "max_dd_pct", "n_trades"] + list(SPACE)[:5]
    print("\nOUT-OF-SAMPLE RESULTS:")
    print(res[cols].to_string(index=False))
    print(f"\n  mean OOS return {res.total_return_pct.mean():+.2f}%  "
          f"vs mean SPY {res.bench_return_pct.mean():+.2f}%  "
          f"-> mean alpha {res.alpha_pct.mean():+.2f}%")
    print(f"  folds with positive alpha: {(res.alpha_pct > 0).sum()}/{len(res)}")
    res.to_csv(ROOT / "research" / "_cache" / f"wf_{obj}.csv", index=False)
