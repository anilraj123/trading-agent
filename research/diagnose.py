"""Sanity battery on the baseline: is the loss real, or an artefact of costs,
the exits, or a bug? Each row is one full 2016-2026 run."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pandas as pd
from research import data, engine, metrics

frames = data.load(); ind = engine.indicators(frames)
_, sectors = data.universe_symbols(); spy = frames["close"]["SPY"]

CASES = [
    ("live config (baseline)",            dict()),
    ("-- cost sensitivity --",            None),
    ("costs 0bps",                        dict(cost_bps=0)),
    ("costs 5bps",                        dict(cost_bps=5)),
    ("costs 20bps",                       dict(cost_bps=20)),
    ("-- is the SCREEN adding value? --", None),
    ("no gates at all (control)",         dict(min_volume_ratio=0, max_move_5d_pct=0,
                                               max_rsi=0, min_rsi=0, require_trend=False)),
    ("trend filter only",                 dict(min_volume_ratio=0, max_move_5d_pct=0,
                                               max_rsi=0, min_rsi=0)),
    ("no volume gate",                    dict(min_volume_ratio=0)),
    ("no RSI cap",                        dict(max_rsi=0)),
    ("-- which EXIT is killing it? --",   None),
    ("no invalidation",                   dict(invalidation_pct=-0.99)),
    ("no invalidation, stop -4%",         dict(invalidation_pct=-0.99, disaster_stop_pct=-0.04)),
    ("no invalidation, stop -2%",         dict(invalidation_pct=-0.99, disaster_stop_pct=-0.02)),
    ("-- let winners run --",             None),
    ("TTL 20d",                           dict(ttl_days=20)),
    ("TTL 60d",                           dict(ttl_days=60)),
    ("TTL 60d, no invalidation",          dict(ttl_days=60, invalidation_pct=-0.99)),
    ("TTL 60d, noinval, stop -4%",        dict(ttl_days=60, invalidation_pct=-0.99,
                                               disaster_stop_pct=-0.04)),
]
rows = []
for name, over in CASES:
    if over is None:
        rows.append({"config": name}); continue
    p = engine.Params(**over)
    t, c = engine.run(frames, ind, p, sectors=sectors)
    m = metrics.summarize(c, t, bench=spy)
    rows.append({"config": name, "ret%": m["total_return_pct"], "cagr%": m["cagr_pct"],
                 "sharpe": m["sharpe"], "maxDD%": m["max_dd_pct"], "trades": m["n_trades"],
                 "win%": m.get("win_rate_pct"), "avgW%": m.get("avg_win_pct"),
                 "avgL%": m.get("avg_loss_pct"), "PF": m.get("profit_factor"),
                 "exp%": m.get("expectancy_pct"), "hold": m.get("avg_hold_days")})
df = pd.DataFrame(rows).fillna("")
print(df.to_string(index=False))
print(f"\nSPY buy & hold same window: +271.88%")
