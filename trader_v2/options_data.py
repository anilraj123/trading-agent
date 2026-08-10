"""Chain fetching for trader_v2 options — the only I/O between the broker and
the pure selection logic in options.py.

Two-call join (verified against alpaca-py 0.40.0): OptionsSnapshot has NO
open_interest, and OptionContract has no quotes/greeks — so contracts (OI,
strike/expiry pre-filter) are joined with snapshots (bid/ask/IV/delta) by OSI
symbol into plain row dicts that select_contract() consumes.
"""
import logging
from datetime import date, timedelta

from trader_v2 import options as opt

logger = logging.getLogger("trader_v2.options_data")

# Strike pre-filter: a 0.40-0.60 delta contract lives near the money; ±20% of
# spot comfortably covers it while keeping the contracts call small.
STRIKE_WINDOW_PCT = 0.20


def _quote_fields(snap):
    """(bid, ask) from a snapshot, tolerating dict or object shapes — the SDK
    has returned both across versions (options_bot lesson)."""
    q = snap.get("latest_quote") if isinstance(snap, dict) else getattr(snap, "latest_quote", None)
    if q is None:
        return None, None
    if isinstance(q, dict):
        return q.get("bid_price") or q.get("bp"), q.get("ask_price") or q.get("ap")
    return getattr(q, "bid_price", None), getattr(q, "ask_price", None)


def _greek_delta(snap):
    g = snap.get("greeks") if isinstance(snap, dict) else getattr(snap, "greeks", None)
    if g is None:
        return None
    return g.get("delta") if isinstance(g, dict) else getattr(g, "delta", None)


def _iv(snap):
    v = snap.get("implied_volatility") if isinstance(snap, dict) else getattr(snap, "implied_volatility", None)
    return float(v) if v else None


def fetch_chain_rows(alpaca, underlying: str, side: str, dte_min: int, dte_max: int,
                     today: date, spot: float = None, feed: str = None) -> list:
    """Normalized chain rows for one underlying and side ("call"/"put").

    Returns [] when the underlying can't be priced or the chain is empty; rows
    with unusable quotes are still emitted (select_contract counts the reason)
    but rows that fail OSI parsing are dropped.
    """
    if spot is None:
        spot = alpaca.get_latest_price(underlying)
    if not spot:
        logger.warning(f"fetch_chain_rows: no spot price for {underlying}")
        return []

    contracts = alpaca.get_option_contracts(
        underlying,
        exp_gte=today + timedelta(days=dte_min),
        exp_lte=today + timedelta(days=dte_max),
        strike_gte=spot * (1 - STRIKE_WINDOW_PCT),
        strike_lte=spot * (1 + STRIKE_WINDOW_PCT),
    )
    want_type = side
    by_symbol = {}
    for c in contracts:
        ctype = c.type.value if hasattr(c.type, "value") else str(c.type)
        if ctype != want_type:
            continue
        by_symbol[c.symbol] = c
    if not by_symbol:
        return []

    snaps = alpaca.get_option_snapshots(list(by_symbol), feed=feed)

    rows = []
    for sym, c in by_symbol.items():
        osi = opt.parse_osi(sym)
        if osi is None:
            continue
        snap = snaps.get(sym)
        bid, ask = _quote_fields(snap) if snap is not None else (None, None)
        rows.append({
            "symbol": sym,
            "type": osi["type"],
            "strike": osi["strike"],
            "expiry": osi["expiry"],
            "dte": opt.dte(osi["expiry"], today),
            "bid": bid,
            "ask": ask,
            "mid": opt.mid_price(bid, ask),
            "delta": _greek_delta(snap) if snap is not None else None,
            "iv": _iv(snap) if snap is not None else None,
            "oi": int(c.open_interest or 0) if c.open_interest is not None else 0,
        })
    return rows
