"""Pure nightly screen: which universe rows are worth the analyst's time.

The lessons file's "validated winning profile" (conviction >= 4, RSI 60-67,
above both SMAs, volume > 1.4x, low fill, non-extended) is turned into a
deterministic pre-qualification here. The analyst then only chooses among
names that already pass — it can no longer be handed a screen full of
extended movers and asked to find the one that isn't.

Rows are the dicts research assembles per symbol:
  {symbol, close, rsi_14, sma_20, sma_50, volume_ratio, chg_5d_pct, sector, ...}
"""
from . import thesis as th


def profile_of(row: dict, *, min_volume_ratio: float, max_move_5d_pct: float,
               max_rsi: float, min_rsi: float, require_trend: bool = True):
    """'bullish' | 'bearish' | None for one screen row.

    bullish: passes the hard gates, RSI in [min_rsi, max_rsi], and (if
             require_trend) close above BOTH SMAs.
    bearish: the mirror — hard gates (bearish), RSI in [100-max, 100-min],
             close below both SMAs.
    A row missing close/RSI/either SMA is never qualified (unknown != pass;
    this is a screen, not a gate)."""
    close, rsi = row.get("close"), row.get("rsi_14")
    sma20, sma50 = row.get("sma_20"), row.get("sma_50")
    if close is None or rsi is None or (require_trend and (sma20 is None or sma50 is None)):
        return None
    gates = dict(min_volume_ratio=min_volume_ratio, max_move_5d_pct=max_move_5d_pct,
                 max_rsi=max_rsi)
    if th.hard_gate_reason("bullish", row, **gates) is None and min_rsi <= rsi <= max_rsi \
            and (not require_trend or (close > sma20 and close > sma50)):
        return "bullish"
    if th.hard_gate_reason("bearish", row, **gates) is None \
            and (100 - max_rsi) <= rsi <= (100 - min_rsi) \
            and (not require_trend or (close < sma20 and close < sma50)):
        return "bearish"
    return None


def rank(rows: list, n_bullish: int, n_bearish: int) -> list:
    """Qualified rows (each stamped with `profile`), strongest volume
    confirmation first within each profile, truncated per profile. Rows
    without a profile are dropped."""
    bull = sorted([r for r in rows if r.get("profile") == "bullish"],
                  key=lambda r: -(r.get("volume_ratio") or 0))
    bear = sorted([r for r in rows if r.get("profile") == "bearish"],
                  key=lambda r: -(r.get("volume_ratio") or 0))
    return bull[:max(0, n_bullish)] + bear[:max(0, n_bearish)]
