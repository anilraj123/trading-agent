"""Volatility-scaled version of the user's design: trail = hwm - N x ATR(14).

Same rule -- hold while it makes new highs, sell on a retracement from the
peak -- but the band is measured in the stock's OWN daily range rather than a
fixed percentage. A 3% pullback is noise on a name that moves 3% a day and a
genuine breakdown on one that moves 0.8%; a fixed band cannot tell those apart
and gets shaken out of the volatile winners it most needs to hold.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from research import wf_pit

NO_CLOCK = dict(ttl_days=[99999], invalidation_pct=[-0.999],
                trail_activate_pct=[0.0], trail_floor_at_entry=[False],
                disaster_stop_pct=[-0.999], cost_bps=[10.0], trail_stop_pct=[-0.99])

SCREEN_ATR = dict(trail_atr_mult=[1.5, 2.5, 3.5, 5.0, 7.0],
                  max_positions=[4, 8], max_per_sector=[0, 1], **NO_CLOCK)

MOM_ATR = dict(trail_atr_mult=[1.5, 2.5, 3.5, 5.0, 7.0],
               rank_by=["mom_6_1", "mom_12_1"], max_positions=[4, 8],
               require_trend=[True, False], min_volume_ratio=[0.0],
               max_move_5d_pct=[0.0], max_rsi=[0.0], min_rsi=[0.0],
               max_per_sector=[0], **NO_CLOCK)

if __name__ == "__main__":
    a = wf_pit.walk(SCREEN_ATR, "LIVE SCREEN + ATR trailing stop")
    b = wf_pit.walk(MOM_ATR, "MOMENTUM + ATR trailing stop")
    a.to_csv(ROOT/"research"/"_cache"/"wf_atr_screen.csv", index=False)
    b.to_csv(ROOT/"research"/"_cache"/"wf_atr_mom.csv", index=False)
