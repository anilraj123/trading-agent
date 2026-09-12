"""Proves engine_np.run_np reproduces engine.run. A port that silently
diverges would invalidate every result computed with it."""
import sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import numpy as np, pandas as pd
from research import data, engine, engine_np

frames = data.load(); ind = engine.indicators(frames)
_, sectors = data.universe_symbols()
A = engine_np.prepare(frames, ind, sectors)

CASES = [
    ("live config",        engine.Params()),
    ("long hold, no inval",engine.Params(ttl_days=60, invalidation_pct=-0.999)),
    ("trail off",          engine.Params(trail_activate_pct=9.99, ttl_days=20)),
    ("momentum, 8 pos",    engine.Params(rank_by="mom_6_1", ttl_days=40, max_positions=8,
                                         min_volume_ratio=0, max_move_5d_pct=0,
                                         max_rsi=0, min_rsi=0, require_trend=False,
                                         invalidation_pct=-0.999, max_per_sector=0)),
]
ok_all = True
for name, p in CASES:
    t0 = time.time(); tr_pd, curve_pd = engine.run(frames, ind, p, sectors=sectors); t_pd = time.time()-t0
    t0 = time.time(); tr_np, curve_np, dates = engine_np.run_np(A, p); t_np = time.time()-t0
    same_len = len(curve_pd) == len(curve_np)
    maxdiff = np.nanmax(np.abs(curve_pd.to_numpy() - curve_np)) if same_len else float("nan")
    rel = maxdiff / curve_pd.iloc[-1] if same_len else float("nan")
    ok = same_len and len(tr_pd) == len(tr_np) and rel < 1e-9
    ok_all &= ok
    print(f"{'PASS' if ok else 'FAIL'}  {name:22} trades pd={len(tr_pd):5d} np={len(tr_np):5d}  "
          f"final pd={curve_pd.iloc[-1]:12,.2f} np={curve_np[-1]:12,.2f}  "
          f"maxdiff={maxdiff:.2e}  |  {t_pd:6.2f}s -> {t_np:5.2f}s  ({t_pd/t_np:4.1f}x)")
print("\nALL EQUIVALENT" if ok_all else "\n*** DIVERGENCE ***")
sys.exit(0 if ok_all else 1)
