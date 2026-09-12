"""The user's actual design: a PURE trailing-stop system.

  "if a stock is going up, keep it. but at any point if it comes down a
   certain percentage from its closest max, sell it"

That is: arm the trail IMMEDIATELY (trail_activate_pct = 0, so the high-water
mark starts at the entry), exit on an X% retracement from the running peak,
and have NO time-based exit at all. Holding period is decided by price, not a
clock -- losers are cut quickly because the peak is still near the entry,
winners are never truncated.

This was NOT covered by the earlier sweeps, which always armed the trail at
+3% or higher (so no trail existed below that) and paired "trail off" with a
long TTL -- close to the opposite system.

NOTE trail_floor_at_entry MUST be False here: flooring the stop at the entry
would mean a position that never rises can only exit via the disaster stop,
which defeats the design.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pandas as pd
from research import wf_pit

NO_CLOCK = dict(ttl_days=[99999], invalidation_pct=[-0.999],
                trail_activate_pct=[0.0], trail_floor_at_entry=[False],
                cost_bps=[10.0])

SCREEN_TRAIL = dict(trail_stop_pct=[-0.03,-0.05,-0.08,-0.12,-0.20],
                    disaster_stop_pct=[-0.999], max_positions=[4,8],
                    max_per_sector=[0,1], **NO_CLOCK)

MOM_TRAIL = dict(trail_stop_pct=[-0.03,-0.05,-0.08,-0.12,-0.20],
                 rank_by=["mom_6_1","mom_12_1"], disaster_stop_pct=[-0.999],
                 max_positions=[4,8], require_trend=[True,False],
                 min_volume_ratio=[0.0], max_move_5d_pct=[0.0],
                 max_rsi=[0.0], min_rsi=[0.0], max_per_sector=[0], **NO_CLOCK)

if __name__ == "__main__":
    a = wf_pit.walk(SCREEN_TRAIL, "LIVE SCREEN + pure trailing stop (no TTL)")
    b = wf_pit.walk(MOM_TRAIL, "MOMENTUM + pure trailing stop (no TTL)")
    a.to_csv(ROOT/"research"/"_cache"/"wf_trail_screen.csv", index=False)
    b.to_csv(ROOT/"research"/"_cache"/"wf_trail_mom.csv", index=False)
