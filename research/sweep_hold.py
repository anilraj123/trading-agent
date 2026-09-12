"""Sweep 2: TTL was pinned at the grid edge. Extend it and find the ceiling."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pandas as pd
from research import sweep

SPACE = {
    "ttl_days":           [40, 80, 160, 250, 400, 99999],
    "disaster_stop_pct":  [-0.05, -0.08, -0.15, -0.999],
    "invalidation_pct":   [-0.05, -0.999],
    "trail_activate_pct": [0.05, 0.10, 0.25, 9.99],
    "trail_stop_pct":     [-0.03, -0.10, -0.20],
    "cost_bps":           [10.0],
}
combos = sweep.grid(SPACE)
print(f"{len(combos)} configs")
df = sweep.run_grid(combos, procs=18)
df.to_csv(ROOT / "research" / "_cache" / "sweep_hold.csv", index=False)
cols = ["total_return_pct", "cagr_pct", "sharpe", "max_dd_pct", "n_trades",
        "win_rate_pct", "avg_win_pct", "profit_factor", "avg_hold_days"] + list(SPACE)
print("\n=== TOP 12 by Sharpe (return-ranked ties broken) ===")
print(df.sort_values(["sharpe", "total_return_pct"], ascending=False)[cols].head(12).to_string(index=False))
print("\n=== effect of TTL (best config at each TTL, by Sharpe) ===")
print(df.loc[df.groupby("ttl_days").sharpe.idxmax()][cols].to_string(index=False))
print("\n=== effect of trail_activate (best at each) ===")
print(df.loc[df.groupby("trail_activate_pct").sharpe.idxmax()][cols].to_string(index=False))
print(f"\nbeating SPY(+271.88%): {(df.total_return_pct>271.88).sum()}/{len(df)}")
