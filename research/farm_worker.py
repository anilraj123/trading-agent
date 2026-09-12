#!/usr/bin/env python3
"""NUMPY-ONLY sweep worker. Runs on farm nodes, which have no pandas.

  farm_worker.py <cache.npz> <configs.json> [start_iso] [end_iso]

Prints one JSON line per config to stdout -- farm.py captures stdout per task,
so results come back through the log dir rather than being scattered as files
on the nodes. Idempotent: same inputs, same output, safe to re-run when a node
vanishes mid-task.
"""
import json, sys, os
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.params import Params
from research import engine_np


def load(npz_path):
    z = np.load(npz_path, allow_pickle=False)
    A = {k: z[k] for k in ("open", "high", "low", "close", "sector", "syms", "dates")}
    A["n_sectors"] = int(z["n_sectors"])
    A["ind"] = {k[4:]: z[k] for k in z.files if k.startswith("ind_")}
    return A, (z["membership"] if "membership" in z.files else None), z["spy"]


def summarize(curve, trades, spy_slice):
    if len(curve) < 2:
        return {}
    r = np.diff(curve) / curve[:-1]
    total = curve[-1] / curve[0] - 1
    yrs = max(len(curve) / 252.0, 1e-9)
    dd = curve / np.maximum.accumulate(curve) - 1
    pnl = np.array([t["pnl"] for t in trades]) if trades else np.array([])
    pct = np.array([t["pnl_pct"] for t in trades]) if trades else np.array([])
    w, l = pnl[pnl > 0], pnl[pnl <= 0]
    sd = r.std()
    out = {
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(((1 + total) ** (1 / yrs) - 1) * 100, 2),
        "sharpe": round(float(r.mean() / sd * np.sqrt(252)), 2) if sd > 0 else 0.0,
        "max_dd_pct": round(float(dd.min()) * 100, 2),
        "n_trades": len(trades),
        "win_rate_pct": round(float((pnl > 0).mean()) * 100, 1) if len(pnl) else None,
        "avg_win_pct": round(float(pct[pnl > 0].mean()) * 100, 2) if len(w) else None,
        "avg_loss_pct": round(float(pct[pnl <= 0].mean()) * 100, 2) if len(l) else None,
        "profit_factor": round(float(w.sum() / abs(l.sum())), 2) if len(l) and l.sum() else None,
        "expectancy_pct": round(float(pct.mean()) * 100, 3) if len(pct) else None,
        "avg_hold_days": round(float(np.mean([t["held"] for t in trades])), 1) if trades else None,
    }
    if spy_slice is not None and len(spy_slice) > 1 and np.isfinite(spy_slice[[0, -1]]).all():
        b = spy_slice[-1] / spy_slice[0] - 1
        out["bench_return_pct"] = round(b * 100, 2)
        out["alpha_pct"] = round((total - b) * 100, 2)
    return out


def main():
    npz, cfg_path = sys.argv[1], sys.argv[2]
    lo = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "-" else None
    hi = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] != "-" else None
    A, mask, spy = load(npz)
    dates = A["dates"]
    si = 60 if lo is None else max(60, int(np.searchsorted(dates,
              np.datetime64(lo, "D").astype("int64"))))
    ei = None if hi is None else int(np.searchsorted(dates,
              np.datetime64(hi, "D").astype("int64")))
    for cfg in json.loads(open(cfg_path).read()):
        p = Params(**cfg)
        trades, curve, _ = engine_np.run_np(A, p, start_i=si, end_i=ei, membership=mask)
        m = summarize(curve, trades, spy[si:(ei if ei is not None else len(spy))])
        m.update(cfg); m["_start"], m["_end"] = lo, hi
        print(json.dumps(m), flush=True)


if __name__ == "__main__":
    main()
