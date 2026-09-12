"""Params lives here, apart from engine.py, so the numpy-only execution path
can import it WITHOUT pulling in pandas -- the farm nodes have numpy but no
pandas, so any pandas import at module scope makes the engine undistributable.
"""
from dataclasses import dataclass, asdict


@dataclass
class Params:
    # entry screen
    min_volume_ratio: float = 1.4          # V2_GATE_MIN_VOLUME_RATIO
    max_move_5d_pct: float = 7.0           # V2_GATE_MAX_MOVE_5D_PCT
    max_rsi: float = 67.0                  # V2_GATE_MAX_RSI
    min_rsi: float = 50.0                  # V2_SCREEN_MIN_RSI
    require_trend: bool = True             # close above BOTH SMAs
    # portfolio
    max_positions: int = 4                 # V2_MAX_POSITIONS
    max_per_sector: int = 1                # V2_MAX_PER_SECTOR (0 = off)
    # exits
    disaster_stop_pct: float = -0.08       # V2_DISASTER_STOP_PCT
    trail_activate_pct: float = 0.03       # V2_TRAIL_ACTIVATE_PCT
    trail_stop_pct: float = -0.03          # V2_TRAIL_STOP_PCT
    trail_floor_at_entry: bool = True      # V2_TRAIL_FLOOR_AT_ENTRY
    ttl_days: int = 5                      # thesis TTL in trading days
    invalidation_pct: float = -0.05        # proxy for the analyst's invalidation
    # frictions
    cost_bps: float = 10.0                 # per side, in basis points
    # ranking
    rank_by: str = "volume_ratio"

    def key(self):
        return tuple(sorted(asdict(self).items()))
