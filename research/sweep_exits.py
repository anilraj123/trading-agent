"""Sweep 1: the exit system, holding the live screen fixed.

The diagnostics say the exits destroy ~260 points of return versus never
selling, so search there first and search it widely.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pandas as pd
from research import sweep

SPACE = {
    "ttl_days":           [5, 10, 20, 40, 80, 160],
    "disaster_stop_pct":  [-0.03, -0.05, -0.08, -0.12, -0.20],
    "invalidation_pct":   [-0.03, -0.05, -0.99],          # -0.99 = off
    "trail_activate_pct": [0.03, 0.05, 0.10, 9.99],       # 9.99 = trail off
    "trail_stop_pct":     [-0.015, -0.03, -0.06, -0.10],
    "cost_bps":           [10.0],
}
combos = sweep.grid(SPACE)
print(f"{len(combos)} configs, full history 2016-2026")
df = sweep.run_grid(combos, procs=18)
df.to_csv(ROOT / "research" / "_cache" / "sweep_exits.csv", index=False)

cols = ["total_return_pct", "cagr_pct", "sharpe", "max_dd_pct", "n_trades",
        "win_rate_pct", "avg_win_pct", "avg_loss_pct", "profit_factor",
        "expectancy_pct", "avg_hold_days"] + list(SPACE)
print("\n=== TOP 15 by total return ===")
print(df.sort_values("total_return_pct", ascending=False)[cols].head(15).to_string(index=False))
print("\n=== TOP 10 by Sharpe ===")
print(df.sort_values("sharpe", ascending=False)[cols].head(10).to_string(index=False))
print(f"\nconfigs beating SPY (+271.88%): {(df.total_return_pct > 271.88).sum()} / {len(df)}")
print(f"configs profitable at all:       {(df.total_return_pct > 0).sum()} / {len(df)}")
