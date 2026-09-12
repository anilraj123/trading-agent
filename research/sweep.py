"""Parallel parameter sweep with walk-forward validation.

Workers inherit the (read-only) price frames via fork, so the 66MB cache is
mapped once, not copied per process.

WHY WALK-FORWARD: a grid search over 10 years of one biased universe will
always find something that looks good. The only number worth quoting is
out-of-sample. `walk_forward` picks the best config on each TRAIN window by
the chosen objective and scores it on the NEXT, unseen, TEST window.
"""
import itertools, os, sys, time
from dataclasses import asdict, replace
from multiprocessing import Pool
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from research import data, engine, metrics

_G = {}


def init():
    if _G:
        return
    _G["frames"] = data.load()
    _G["ind"] = engine.indicators(_G["frames"])
    _, _G["sectors"] = data.universe_symbols()
    _G["spy"] = _G["frames"]["close"]["SPY"]


def _one(job):
    over, start, end = job
    init()
    p = engine.Params(**over)
    t, c = engine.run(_G["frames"], _G["ind"], p, sectors=_G["sectors"],
                      start=start, end=end)
    m = metrics.summarize(c, t, bench=_G["spy"])
    m.update(over)
    m["_start"], m["_end"] = start, end
    return m


def grid(space: dict):
    keys = list(space)
    return [dict(zip(keys, v)) for v in itertools.product(*(space[k] for k in keys))]


def run_grid(combos, start=None, end=None, procs=None, label=""):
    procs = procs or max(1, os.cpu_count() - 2)
    jobs = [(c, start, end) for c in combos]
    t0 = time.time()
    init()                                   # parent loads once; fork shares it
    with Pool(procs) as pool:
        rows = []
        for i, r in enumerate(pool.imap_unordered(_one, jobs, chunksize=4), 1):
            rows.append(r)
            if i % 200 == 0 or i == len(jobs):
                el = time.time() - t0
                print(f"  {label}{i}/{len(jobs)}  {el:6.1f}s  "
                      f"({i/el:.1f}/s, eta {(len(jobs)-i)/(i/el):5.0f}s)", flush=True)
    return pd.DataFrame(rows)


def walk_forward(combos, folds, objective="sharpe", procs=None):
    """folds: [(train_start, train_end, test_start, test_end), ...]
    Returns (per-fold winners with their OUT-OF-SAMPLE scores, all train rows)."""
    out, trains = [], []
    for i, (ts, te, vs, ve) in enumerate(folds, 1):
        print(f"\nfold {i}/{len(folds)}  train {ts}..{te}  test {vs}..{ve}")
        tr = run_grid(combos, ts, te, procs, label="train ")
        tr = tr[tr.n_trades >= 30]
        if tr.empty:
            print("  no config traded enough in train; skipping fold")
            continue
        best = tr.sort_values(objective, ascending=False).iloc[0]
        keys = list(combos[0])
        chosen = {k: best[k] for k in keys}
        te_row = _one((chosen, vs, ve))
        print(f"  chosen: { {k: chosen[k] for k in keys} }")
        print(f"  train {objective} {best[objective]}  ret {best.total_return_pct}%"
              f"   ->  TEST ret {te_row['total_return_pct']}%  sharpe {te_row['sharpe']}"
              f"  (SPY {te_row.get('bench_return_pct')}%)")
        te_row["fold"] = i
        te_row["train_" + objective] = best[objective]
        out.append(te_row)
        trains.append(tr.assign(fold=i))
    return pd.DataFrame(out), (pd.concat(trains) if trains else pd.DataFrame())
