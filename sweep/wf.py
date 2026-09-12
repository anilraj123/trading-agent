#!/usr/bin/env python3
"""One walk-forward fold: optimise on TRAIN, evaluate the winner on unseen TEST.

This is the honest version of what backtest_daily_walkforward.py gestures at. That
script fixes THRESHOLD = 1.0 and reports the same parameters across three windows,
so every number it prints is in-sample. Here the parameters are *chosen* on train
and never touched again before test, so the test row is a real out-of-sample result.

One fold = a full grid search, so a fold is expensive (seconds to minutes) and folds
are independent. That is the shape the farm wants: coarse, parallel, retryable.

  ./wf.py --cache cache90.npz --train 0:6000 --test 6000:7000 --objective sharpe
"""
import argparse, glob, json, os, subprocess, sys, tempfile, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.expanduser("~/code/ai/farm"))
from run import load, simulate, metrics


def grid_from(spec):
    """'0.2:3.0:10,0.2:3.0:10,-0.005:-0.08:12' -> list of (buy, sell, stop)."""
    axes = []
    for part in spec.split(","):
        a, b, n = part.split(":")
        axes.append(np.linspace(float(a), float(b), int(n)))
    return [(round(float(x), 4), round(float(y), 4), round(float(z), 5))
            for x in axes[0] for y in axes[1] for z in axes[2]]


def span(s):
    lo, hi = s.split(":")
    return int(lo), int(hi)


def evaluate(c, cfg, lo, hi, cost=0.0):
    cap, trades = simulate(c, cfg[0], cfg[1], cfg[2], lo, hi, cost)
    return metrics(cap, trades, c["cap0"])




# ---------------------------------------------------------------- driver mode

def _calibrate(c, grid, tr, te, cost, sample=40):
    """Estimate seconds per fold by timing a slice of the grid and scaling.

    worth_it() needs a real per-task cost, and it varies with grid size, window
    length and machine. Timing ~40 configs costs a fraction of a second and beats
    guessing a constant that goes stale the moment the grid changes."""
    k = min(sample, len(grid))
    t0 = time.time()
    for cfg in grid[:k]:
        evaluate(c, cfg, tr[0], tr[1], cost)
    per_cfg = (time.time() - t0) / k
    return per_cfg * len(grid) + per_cfg      # + the single test-window evaluation


def drive(a):
    import farmlib
    c = load(a.cache)
    T = c["priceM"].shape[0]
    grid = grid_from(a.grid)

    folds = []
    i = 0
    while i + a.train_bars + a.test_bars <= T:
        folds.append((i, i + a.train_bars, i + a.train_bars + a.test_bars))
        i += a.step
    if not folds:
        sys.exit(f"no folds fit: {T} bars < train {a.train_bars} + test {a.test_bars}")

    est = _calibrate(c, grid, (folds[0][0], folds[0][1]), (folds[0][1], folds[0][2]), a.cost_bps / 1e4)
    print(f"== {len(folds)} folds, {len(grid)} configs each, ~{est:.1f}s per fold "
          f"({T} bars in cache)", flush=True)

    f = farmlib.discover()
    ok, why = f.worth_it(len(folds), est)
    if a.force == "fleet":
        ok, why = True, "forced --force fleet"
    elif a.force == "local":
        ok, why = False, "forced --force local"
    print(f"== farm: {len(f.live)}/{len(f.nodes)} nodes, {f.slots} slots -> "
          f"{'FLEET' if ok else 'LOCAL'}: {why}", flush=True)

    target = f if ok else f.local_only()
    if ok and a.sync:
        for addr, good in target.sync([os.path.join(HERE, x) for x in
                                       ("run.py", "wf.py", os.path.basename(a.cache))], HERE):
            print(f"   sync {addr}: {'ok' if good else 'FAILED'}", flush=True)

    tasks = [f'cd {HERE} && python3 wf.py --cache {os.path.basename(a.cache)} '
             f'--train {lo}:{mid} --test {mid}:{hi} --grid {a.grid} '
             f'--objective {a.objective} --min-trades {a.min_trades} '
             f'--cost-bps {a.cost_bps} --fold f{n:02d}'
             for n, (lo, mid, hi) in enumerate(folds)]

    logdir = a.logdir or tempfile.mkdtemp(prefix="wf-")
    t0 = time.time()
    target.run(tasks, logdir=logdir)
    el = time.time() - t0

    rows = []
    for lg in sorted(glob.glob(os.path.join(logdir, "*.log"))):
        for line in open(lg):
            if line.startswith("{"):
                rows.append(json.loads(line))
    _summary(rows, el, logdir, a)


def _summary(rows, el, logdir, a):
    import statistics as st
    ok = [r for r in rows if r.get("status") == "ok"]
    print(f"\n== {len(ok)}/{len(rows)} folds produced a config in {el:.1f}s  (logs: {logdir})")
    if not ok:
        return
    tr = [r["train_metrics"]["gross_return"] for r in ok]
    te = [r["test_metrics"]["gross_return"] for r in ok]
    pos = sum(1 for x in te if x > 0)
    print(f"== cost {a.cost_bps} bps | mean TRAIN {st.mean(tr):+.3f}% | "
          f"mean TEST {st.mean(te):+.3f}% | median TEST {st.median(te):+.3f}% | "
          f"TEST>0 {pos}/{len(te)}")
    print("== TRAIN is best-of-grid and optimistic by construction; read TEST.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cache", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache90.npz"))
    p.add_argument("--train", help="bar range lo:hi (single-fold mode)")
    p.add_argument("--test", help="bar range lo:hi (single-fold mode)")
    p.add_argument("--grid", default="0.2:3.0:10,0.2:3.0:10,-0.005:-0.08:12")
    p.add_argument("--objective", default="sharpe", choices=["sharpe", "gross_return"])
    p.add_argument("--min-trades", type=int, default=20,
                   help="ignore train configs with fewer trades - they are noise, not edge")
    p.add_argument("--cost-bps", type=float, default=0.0,
                   help="one-way cost in basis points, applied to BOTH train and test")
    p.add_argument("--fold", default="")
    # driver mode: generate folds, ask the farm, dispatch
    p.add_argument("--walk", action="store_true", help="driver: run a whole walk-forward")
    p.add_argument("--train-bars", type=int, default=4000)
    p.add_argument("--test-bars", type=int, default=1000)
    p.add_argument("--step", type=int, default=250)
    p.add_argument("--force", choices=["auto", "local", "fleet"], default="auto")
    p.add_argument("--no-sync", dest="sync", action="store_false", default=True)
    p.add_argument("--logdir")
    a = p.parse_args()

    if a.walk:
        return drive(a)
    if not (a.train and a.test):
        p.error("single-fold mode needs --train and --test (or use --walk)")

    c = load(a.cache)
    tr_lo, tr_hi = span(a.train)
    te_lo, te_hi = span(a.test)
    grid = grid_from(a.grid)
    cost = a.cost_bps / 1e4

    best, best_m = None, None
    for cfg in grid:
        m = evaluate(c, cfg, tr_lo, tr_hi, cost)
        if m["trades"] < a.min_trades:
            continue
        if best_m is None or m[a.objective] > best_m[a.objective]:
            best, best_m = cfg, m          # strict > keeps the FIRST of any tie

    if best is None:
        print(json.dumps({"fold": a.fold, "status": "no_config_met_min_trades",
                          "train": a.train, "test": a.test, "grid": len(grid)}))
        return

    test_m = evaluate(c, best, te_lo, te_hi, cost)
    print(json.dumps({
        "fold": a.fold, "status": "ok", "grid": len(grid),
        "train": a.train, "test": a.test,
        "best": {"min_buy": best[0], "min_sell": best[1], "stop_loss": best[2]},
        "train_metrics": best_m, "test_metrics": test_m,
        "objective": a.objective, "cost_bps": a.cost_bps,
    }))


if __name__ == "__main__":
    main()
