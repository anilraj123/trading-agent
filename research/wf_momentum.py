"""Walk-forward the momentum family. In-sample momentum numbers are inflated
by survivorship bias; the question is whether it still beats SPY on unseen
windows, where the bias applies to the benchmark comparison equally."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pandas as pd
from research import sweep

SPACE = {
    "rank_by":            ["mom_12_1", "mom_6_1"],
    "ttl_days":           [20, 40, 60, 120],
    "disaster_stop_pct":  [-0.15, -0.25, -0.999],
    "trail_activate_pct": [0.15, 9.99],
    "trail_stop_pct":     [-0.15],
    "require_trend":      [True, False],
    "max_positions":      [4, 8],
    "min_volume_ratio":   [0.0], "max_move_5d_pct": [0.0],
    "max_rsi":            [0.0], "min_rsi": [0.0],
    "invalidation_pct":   [-0.999], "cost_bps": [10.0],
}
FOLDS = [
    ("2017-01-01", "2019-12-31", "2020-01-01", "2021-12-31"),
    ("2018-01-01", "2021-12-31", "2022-01-01", "2023-12-31"),
    ("2020-01-01", "2023-12-31", "2024-01-01", "2025-12-31"),
    ("2022-01-01", "2025-12-31", "2026-01-01", "2026-09-11"),
]
combos = sweep.grid(SPACE)
print(f"{len(combos)} configs x {len(FOLDS)} folds")
res, _ = sweep.walk_forward(combos, FOLDS, objective="sharpe", procs=18)
cols = ["fold", "_start", "_end", "total_return_pct", "bench_return_pct", "alpha_pct",
        "sharpe", "max_dd_pct", "n_trades", "rank_by", "ttl_days", "max_positions",
        "require_trend", "disaster_stop_pct"]
print("\nMOMENTUM — OUT OF SAMPLE:")
print(res[cols].to_string(index=False))
print(f"\n  mean OOS {res.total_return_pct.mean():+.2f}%  vs SPY {res.bench_return_pct.mean():+.2f}%"
      f"  -> alpha {res.alpha_pct.mean():+.2f}%")
print(f"  folds with positive alpha: {(res.alpha_pct>0).sum()}/{len(res)}")
res.to_csv(ROOT / "research" / "_cache" / "wf_momentum.csv", index=False)
