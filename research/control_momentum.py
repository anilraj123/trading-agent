"""Harness control: can this engine detect edge that is KNOWN to exist?

12-1 momentum (buy past winners, skipping the most recent month) is one of the
most replicated anomalies in the literature. If the harness cannot show it
working, the harness is suspect and the screen's failure proves nothing. If it
CAN, then the screen's failure is a statement about the screen.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pandas as pd
from research import sweep

OFF = dict(min_volume_ratio=0, max_move_5d_pct=0, max_rsi=0, min_rsi=0)
BASE = dict(invalidation_pct=-0.999, trail_activate_pct=9.99, disaster_stop_pct=-0.999)
CASES = [
    ("12-1 momentum, 60d hold, trend",  dict(rank_by="mom_12_1", ttl_days=60, require_trend=True,  **OFF, **BASE)),
    ("12-1 momentum, 60d hold, notrend",dict(rank_by="mom_12_1", ttl_days=60, require_trend=False, **OFF, **BASE)),
    ("12-1 momentum, 120d hold",        dict(rank_by="mom_12_1", ttl_days=120, require_trend=False, **OFF, **BASE)),
    ("6-1 momentum, 60d hold",          dict(rank_by="mom_6_1",  ttl_days=60, require_trend=False, **OFF, **BASE)),
    ("low-vol, 60d hold",               dict(rank_by="lowvol",   ttl_days=60, require_trend=False, **OFF, **BASE)),
    ("LIVE screen, 60d hold",           dict(rank_by="volume_ratio", ttl_days=60, **BASE)),
    ("LIVE screen, live exits",         dict()),
]
rows = []
for name, over in CASES:
    m = sweep._one((over, None, None))
    rows.append({"strategy": name, "ret%": m["total_return_pct"], "cagr%": m["cagr_pct"],
                 "sharpe": m["sharpe"], "maxDD%": m["max_dd_pct"], "trades": m["n_trades"],
                 "win%": m.get("win_rate_pct"), "PF": m.get("profit_factor"),
                 "alpha%": m.get("alpha_pct")})
print("FULL HISTORY 2016-2026 (SPY +271.88%)\n")
print(pd.DataFrame(rows).to_string(index=False))
