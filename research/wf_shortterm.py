"""Short-horizon entry families: mean reversion and gap-fade.

Both BUY WEAKNESS -- the opposite sign to every family tested so far, all of
which bought strength and all of which lost. Exits stay short by construction
(TTL 1-10 days), matching the stated requirement of a short-term system.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from research import wf_pit

COMMON = dict(invalidation_pct=[-0.999], trail_activate_pct=[9.99],
              trail_floor_at_entry=[False], max_per_sector=[0],
              min_volume_ratio=[0.0], max_move_5d_pct=[0.0],
              max_rsi=[0.0], min_rsi=[0.0], cost_bps=[10.0])

MEANREV = dict(signal=["meanrev"], rank_by=["rsi_2"],
               rsi2_max=[5.0, 10.0, 15.0], dist_sma20_max=[0.0, -3.0],
               trend_filter_sma200=[True, False],
               ttl_days=[1, 2, 3, 5, 10],
               disaster_stop_pct=[-0.07, -0.999],
               max_positions=[4, 8], **COMMON)

GAPFADE = dict(signal=["gapfade"], rank_by=["gap_pct"],
               gap_max_pct=[-2.0, -3.0, -5.0, -8.0],
               trend_filter_sma200=[True, False],
               ttl_days=[1, 2, 3, 5],
               disaster_stop_pct=[-0.07, -0.999],
               max_positions=[4, 8], **COMMON)

if __name__ == "__main__":
    a = wf_pit.walk(MEANREV, "MEAN REVERSION (buy RSI(2) oversold)")
    a.to_csv(ROOT/"research"/"_cache"/"wf_meanrev.csv", index=False)
    b = wf_pit.walk(GAPFADE, "GAP FADE (buy down-gaps at the open)")
    b.to_csv(ROOT/"research"/"_cache"/"wf_gapfade.csv", index=False)
