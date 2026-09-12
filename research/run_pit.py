"""THE decisive test: re-run the key strategies on a point-in-time universe.

Everything in research/README.md so far was computed on TODAY's index
membership, which silently excludes the 600+ companies the index dropped.
This reruns the same strategies where a name is only buyable on bars when it
was ACTUALLY an index member, using bars that include the casualties.

Expectation going in: momentum's alpha should shrink hard, because buying past
winners inside a winners-only universe is close to look-ahead. If it survives
here, it is worth building on. If it collapses, better to learn it now.
"""
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import numpy as np, pandas as pd
from research import data, engine, engine_np, metrics, pit_data

MOM = dict(rank_by="mom_6_1", ttl_days=40, max_positions=8, require_trend=True,
           min_volume_ratio=0, max_move_5d_pct=0, max_rsi=0, min_rsi=0,
           invalidation_pct=-0.999, disaster_stop_pct=-0.999,
           trail_activate_pct=9.99, max_per_sector=0)
CASES = [
    ("live config",              dict()),
    ("live screen, 60d hold",    dict(ttl_days=60, invalidation_pct=-0.999,
                                      disaster_stop_pct=-0.999, trail_activate_pct=9.99)),
    ("6-1 momentum (wf winner)", MOM),
]
FOLDS = [("2020-01-01", "2021-12-31"), ("2022-01-01", "2023-12-31"),
         ("2024-01-01", "2025-12-31"), ("2026-01-01", "2026-09-11")]


def build(frames, sectors, use_mask):
    ind = engine.indicators(frames)
    A = engine_np.prepare(frames, ind, sectors)
    mask = pit_data.membership_mask(frames["close"].index, list(frames["close"].columns)) \
        if use_mask else None
    return A, mask


def score(A, mask, p, spy, lo=None, hi=None):
    dates = pd.DatetimeIndex(A["dates"])
    si = 60 if lo is None else max(60, int(dates.searchsorted(pd.Timestamp(lo))))
    ei = None if hi is None else int(dates.searchsorted(pd.Timestamp(hi)))
    tr, curve, dd = engine_np.run_np(A, p, start_i=si, end_i=ei, membership=mask)
    if len(curve) < 2:
        return {}
    c = pd.Series(curve, index=pd.DatetimeIndex(dd))
    t = pd.DataFrame(tr) if tr else pd.DataFrame(columns=["pnl", "pnl_pct", "held", "reason"])
    return metrics.summarize(c, t, bench=spy)


def main():
    rows = []
    for label, use_mask, loader in (("BIASED (today's index)", False, data.load),
                                    ("POINT-IN-TIME", True, pit_data.load)):
        frames = loader()
        _, sectors = data.universe_symbols()
        spy = frames["close"]["SPY"]
        A, mask = build(frames, sectors, use_mask)
        n_sym = frames["close"].shape[1]
        print(f"\n{'='*80}\n{label}   ({n_sym} symbols)\n{'='*80}")
        for name, over in CASES:
            p = engine.Params(**over)
            m = score(A, mask, p, spy)
            rows.append(dict(universe=label, strategy=name, **{
                k: m.get(k) for k in ("total_return_pct", "cagr_pct", "sharpe",
                                      "max_dd_pct", "n_trades", "win_rate_pct",
                                      "profit_factor", "alpha_pct")}))
            print(f"  {name:26} ret {m.get('total_return_pct'):>9}%  "
                  f"sharpe {m.get('sharpe'):>5}  DD {m.get('max_dd_pct'):>7}%  "
                  f"trades {m.get('n_trades'):>5}  alpha {m.get('alpha_pct'):>9}%")
        if use_mask:
            print("\n  per-period, point-in-time (same params, no refitting):")
            for name, over in CASES:
                p = engine.Params(**over)
                line = []
                for lo, hi in FOLDS:
                    m = score(A, mask, p, spy, lo, hi)
                    line.append(f"{lo[:4]}-{hi[2:4]}: {m.get('alpha_pct', float('nan')):+7.1f}")
                print(f"    {name:26} alpha  " + "  ".join(line))
    df = pd.DataFrame(rows)
    df.to_csv(ROOT / "research" / "_cache" / "pit_comparison.csv", index=False)
    print(f"\n{'='*80}\nBIAS = biased minus point-in-time, per strategy\n{'='*80}")
    piv = df.pivot(index="strategy", columns="universe", values="total_return_pct")
    piv["bias_pts"] = piv.iloc[:, 0] - piv.iloc[:, 1]
    print(piv.to_string())


if __name__ == "__main__":
    main()
