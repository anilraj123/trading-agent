#!/usr/bin/env python3
"""Check run.py's vectorised simulate() against a literal transcription of the
original run() loop, on the same cache. The precompute is executed from the
original file so it cannot drift; this checks the one thing that was rewritten."""
import sys, os, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run import load, simulate, metrics

NEG_INF = -np.inf


def reference(c, min_buy, min_sell, stop_loss):
    """Literal transcription of the original: per-symbol candidate scan + max()."""
    priceM, sellM, buyM, hasbar = c["priceM"], c["sellM"], c["buyM"], c["hasbar"]
    T, S = priceM.shape
    cap, position, trades = c["cap0"], None, []
    for t in range(T):
        if position is not None:
            s = position["col"]
            if not hasbar[t, s]:
                continue
            price = priceM[t, s]; ss = sellM[t, s]
            stop = position["entry_price"] * (1 + stop_loss)
            if ss >= min_sell or price <= stop:
                exit_p = stop if price <= stop else price
                pnl = (exit_p - position["entry_price"]) / position["entry_price"]
                cap *= 1 + pnl
                trades.append({"sym": c["symbols"][s], "pnl_pct": round(pnl * 100, 2),
                               "win": pnl > 0, "buy_score": position["buy_score"],
                               "sell_score": float(ss), "hit_stop": bool(price <= stop)})
                position = None
        if position is None:
            candidates = []
            for s in range(S):
                if not hasbar[t, s]:
                    continue
                bs = buyM[t, s]
                if bs == NEG_INF:        # idx < WARMUP in the original
                    continue
                if bs < min_buy:
                    continue
                candidates.append((s, priceM[t, s], bs))
            if candidates:
                best = max(candidates, key=lambda x: x[2])
                position = {"col": best[0], "entry_price": best[1], "buy_score": float(best[2])}
    return cap, trades


c = load(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.npz"))
grid = [(t, 1.0, -0.03) for t in (0.5, 1.0, 1.5, 2.0, 3.0)]
grid += [(1.0, 0.5, -0.03), (1.5, 2.0, -0.05), (2.0, 1.0, -0.01)]

bad = 0
for mb, ms, sl in grid:
    cap_v, tr_v = simulate(c, mb, ms, sl)
    cap_r, tr_r = reference(c, mb, ms, sl)
    mv, mr = metrics(cap_v, tr_v, c["cap0"]), metrics(cap_r, tr_r, c["cap0"])
    same = (len(tr_v) == len(tr_r)
            and all(a["sym"] == b["sym"] and a["pnl_pct"] == b["pnl_pct"]
                    and a["hit_stop"] == b["hit_stop"] for a, b in zip(tr_v, tr_r))
            and abs(mv["gross_return"] - mr["gross_return"]) < 1e-9
            and abs(mv["sharpe"] - mr["sharpe"]) < 1e-12)
    print(f"buy={mb:<4} sell={ms:<4} stop={sl:<6} "
          f"trades {len(tr_v):>4}/{len(tr_r):<4} ret {mv['gross_return']:+8.4f}  "
          f"{'MATCH' if same else '*** MISMATCH ***'}")
    bad += 0 if same else 1
print(f"\n{len(grid)-bad}/{len(grid)} configs identical (trade-for-trade)")
sys.exit(1 if bad else 0)
