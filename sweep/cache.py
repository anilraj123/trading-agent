#!/usr/bin/env python3
"""Stage 1 of the cache-then-sweep split: fetch once, precompute once.

Runs the ORIGINAL backtest_5min_sweep.py's own fetch + TA precompute by executing
its source up to the sweep, so the numbers here are by construction identical to
the original and stay in step if that file is edited. Everything after the
precompute is parameter search, which sweep/run.py does per-config.

Output is a single .npz of dense [time x symbol] matrices, ~5 MB, which is small
enough to ship to every node in well under a second at the fleet's ~220 Mbit/s.
"""
import argparse, os, sys, numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
ORIG = os.path.join(PROJ, "backtest_5min_sweep.py")
SPLIT = "all_times = sorted(all_times)"


def build(days=None, out=None):
    src = open(ORIG).read()
    if days:
        # Widen the fetch window without editing the original. Walk-forward needs
        # more history than the original's 20 days to give disjoint train/test folds.
        import re
        src, n = re.subn(r"^DAYS = \d+", f"DAYS = {days}", src, count=1, flags=re.M)
        if not n:
            sys.exit("could not find 'DAYS = <n>' to override")
    if SPLIT not in src:
        sys.exit(f"marker {SPLIT!r} not found in {ORIG} - the original changed shape")
    prefix = src.split(SPLIT)[0] + SPLIT

    os.chdir(PROJ)                      # .env and trader/ resolve relative to the project
    ns = {"__name__": "__cache_build__", "__file__": ORIG}
    print("== running original fetch + precompute ...", flush=True)
    exec(compile(prefix, ORIG, "exec"), ns)

    data, all_times = ns["data"], ns["all_times"]
    WARMUP, CAP = ns["WARMUP"], ns["INITIAL_CAPITAL"]
    syms = list(data.keys())            # insertion order: argmax ties must match max()
    T, S = len(all_times), len(syms)
    print(f"== {S} symbols x {T} bars", flush=True)

    priceM = np.full((T, S), np.nan)
    sellM  = np.full((T, S), np.nan)
    buyM   = np.full((T, S), -np.inf)   # -inf so warmup/missing never win argmax
    hasbar = np.zeros((T, S), dtype=bool)

    for s, sym in enumerate(syms):
        e = data[sym]
        pos = e["index"].get_indexer(all_times)   # same call the original makes per-bar
        ok = pos >= 0
        hasbar[ok, s] = True
        priceM[ok, s] = e["prices"][pos[ok]]
        sellM[ok, s]  = e["sell_scores"][pos[ok]]
        warm = ok & (pos >= WARMUP)               # buy side requires idx >= WARMUP
        buyM[warm, s] = e["buy_scores"][pos[warm]]

    out = out or os.path.join(HERE, "cache.npz")
    np.savez_compressed(out, priceM=priceM, sellM=sellM, buyM=buyM, hasbar=hasbar,
                        symbols=np.array(syms), warmup=WARMUP, initial_capital=CAP,
                        times=np.array([np.datetime64(t) for t in all_times]))
    print(f"== wrote {out} ({os.path.getsize(out)/1e6:.1f} MB)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, help="override the original's DAYS lookback")
    ap.add_argument("--out")
    a = ap.parse_args()
    build(a.days, a.out)
