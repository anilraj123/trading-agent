"""Walk-forward on the POINT-IN-TIME universe. The only test whose answer
should be allowed near real money: parameters chosen on a train window, scored
on a later window that was never looked at, in a universe that includes the
companies the index threw away.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import numpy as np, pandas as pd
from research import data, engine, engine_np, metrics, pit_data

FOLDS = [("2016-04-01","2019-12-31","2020-01-01","2021-12-31"),
         ("2018-01-01","2021-12-31","2022-01-01","2023-12-31"),
         ("2020-01-01","2023-12-31","2024-01-01","2025-12-31"),
         ("2022-01-01","2025-12-31","2026-01-01","2026-09-11")]

MOM_SPACE = dict(rank_by=["mom_12_1","mom_6_1"], ttl_days=[20,40,60,120],
                 max_positions=[4,8], require_trend=[True,False],
                 disaster_stop_pct=[-0.15,-0.999], trail_activate_pct=[0.15,9.99],
                 trail_stop_pct=[-0.15], invalidation_pct=[-0.999],
                 min_volume_ratio=[0.0], max_move_5d_pct=[0.0], max_rsi=[0.0],
                 min_rsi=[0.0], max_per_sector=[0], cost_bps=[10.0])
SCREEN_SPACE = dict(ttl_days=[10,40,120], disaster_stop_pct=[-0.05,-0.08,-0.15],
                    invalidation_pct=[-0.05,-0.999], trail_activate_pct=[0.05,9.99],
                    trail_stop_pct=[-0.03,-0.10], cost_bps=[10.0])

_G = {}
def init():
    if _G: return
    frames = pit_data.load()
    _, sectors = data.universe_symbols()
    ind = engine.indicators(frames)
    _G["A"] = engine_np.prepare(frames, ind, sectors)
    _G["mask"] = pit_data.membership_mask(frames["close"].index, list(frames["close"].columns))
    _G["spy"] = frames["close"]["SPY"]
    _G["dates"] = pd.DatetimeIndex(_G["A"]["dates"])

def score(cfg, lo, hi):
    init()
    d = _G["dates"]
    si = max(60, int(d.searchsorted(pd.Timestamp(lo))))
    ei = int(d.searchsorted(pd.Timestamp(hi)))
    tr, curve, dd = engine_np.run_np(_G["A"], engine.Params(**cfg),
                                     start_i=si, end_i=ei, membership=_G["mask"])
    if len(curve) < 2: return {}
    c = pd.Series(curve, index=pd.DatetimeIndex(dd))
    t = pd.DataFrame(tr) if tr else pd.DataFrame(columns=["pnl","pnl_pct","held","reason"])
    m = metrics.summarize(c, t, bench=_G["spy"]); m.update(cfg); return m

def _job(a): return score(*a)

def grid(space):
    import itertools
    k = list(space)
    return [dict(zip(k, v)) for v in itertools.product(*(space[x] for x in k))]

def walk(space, name, objective="sharpe"):
    from multiprocessing import Pool
    combos = grid(space)
    print(f"\n{'='*78}\n{name}: {len(combos)} configs x {len(FOLDS)} folds (point-in-time)\n{'='*78}")
    init()
    rows = []
    for i,(ts,te,vs,ve) in enumerate(FOLDS,1):
        with Pool(18) as pool:
            tr = pd.DataFrame(pool.map(_job, [(c,ts,te) for c in combos], chunksize=2))
        tr = tr[tr.n_trades >= 20]
        if tr.empty: print(f"  fold {i}: nothing traded enough"); continue
        best = tr.sort_values(objective, ascending=False).iloc[0]
        cfg = {k: best[k] for k in combos[0]}
        m = score(cfg, vs, ve); m["fold"]=i
        rows.append(m)
        keys = {k:v for k,v in cfg.items() if k in ("rank_by","ttl_days","max_positions","require_trend","disaster_stop_pct","invalidation_pct","trail_activate_pct")}
        print(f"  fold {i} test {vs[:7]}..{ve[:7]}  chose {keys}")
        print(f"     TEST ret {m['total_return_pct']:+8.2f}%   SPY {m.get('bench_return_pct'):+7.2f}%"
              f"   alpha {m.get('alpha_pct'):+8.2f}%   sharpe {m['sharpe']:+5.2f}"
              f"   DD {m['max_dd_pct']:+7.2f}%   trades {m['n_trades']}")
    r = pd.DataFrame(rows)
    if not r.empty:
        print(f"\n  MEAN OOS alpha {r.alpha_pct.mean():+.2f}%   folds positive {(r.alpha_pct>0).sum()}/{len(r)}"
              f"   median alpha {r.alpha_pct.median():+.2f}%")
    return r

if __name__ == "__main__":
    a = walk(MOM_SPACE, "MOMENTUM family")
    b = walk(SCREEN_SPACE, "LIVE SCREEN family")
    a.to_csv(ROOT/"research"/"_cache"/"wf_pit_momentum.csv", index=False)
    b.to_csv(ROOT/"research"/"_cache"/"wf_pit_screen.csv", index=False)
