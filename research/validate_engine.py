"""Engine validation: with exits disabled the simulator must behave like
buy-and-hold of a rotating 4-name basket. If THAT doesn't track the market,
the engine is broken and every other result is meaningless."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pandas as pd
from research import data, engine, metrics

frames = data.load(); ind = engine.indicators(frames)
_, sectors = data.universe_symbols(); spy = frames["close"]["SPY"]

NO_EXIT = dict(ttl_days=99999, invalidation_pct=-0.999,
               disaster_stop_pct=-0.999, trail_activate_pct=9.99)
CASES = [
    ("screen + NEVER sell, 0bps",      dict(cost_bps=0, **NO_EXIT)),
    ("screen + NEVER sell, 10bps",     dict(cost_bps=10, **NO_EXIT)),
    ("no gates + NEVER sell, 0bps",    dict(cost_bps=0, min_volume_ratio=0, max_move_5d_pct=0,
                                            max_rsi=0, min_rsi=0, require_trend=False, **NO_EXIT)),
    ("screen + 250d TTL only, 0bps",   dict(cost_bps=0, ttl_days=250, invalidation_pct=-0.999,
                                            disaster_stop_pct=-0.999, trail_activate_pct=9.99)),
]
rows = []
for name, over in CASES:
    p = engine.Params(**over)
    t, c = engine.run(frames, ind, p, sectors=sectors)
    m = metrics.summarize(c, t, bench=spy)
    rows.append({"config": name, "ret%": m["total_return_pct"], "cagr%": m["cagr_pct"],
                 "sharpe": m["sharpe"], "maxDD%": m["max_dd_pct"],
                 "trades": m["n_trades"], "hold_d": m.get("avg_hold_days"),
                 "win%": m.get("win_rate_pct")})
print(pd.DataFrame(rows).to_string(index=False))
print("\nSPY buy & hold 2016-04 -> 2026-09: +271.88%  (CAGR ~13.4%)")
print("\nIf 'NEVER sell' lands in the same ballpark as SPY, the engine is sound")
print("and the baseline's -71% is the EXITS + turnover, not a simulator bug.")
