"""Deterministic backtest of the trader_v2 mechanical core.

WHAT THIS TESTS: the screen (trader_v2/screen.py) as an entry signal plus the
rule-based exits (trader_v2/thesis.exit_decision). The LLM analyst is REMOVED.

That is deliberate. The analyst cannot be replayed, and 6 of the first 11 live
trades exited via `research_close` -- the analyst deciding days later that its
own thesis had broken. This harness answers the prior question: does the
mechanical profile the analyst picks FROM have any edge at all? If it does
not, no amount of LLM curation on top will save it.

Indicator definitions are copied from trader/technical_analysis.py EXACTLY,
including the 2-decimal rounding -- which is not cosmetic, because gates
compare against thresholds like volume_ratio >= 1.4 where a hair either side
flips the decision.
"""
from dataclasses import asdict

from research.params import Params  # noqa: F401  (re-exported)

import numpy as np
import pandas as pd


# --- indicators: must mirror trader/technical_analysis.py -------------------

def rsi_wide(close: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """SIMPLE-MA RSI, as the live bot computes it (NOT Wilder's smoothing)."""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.round(2)


def indicators(frames: dict) -> dict:
    close, volume = frames["close"], frames["volume"]
    return {
        "rsi_14": rsi_wide(close, 14),
        "sma_20": close.rolling(20).mean().round(2),
        "sma_50": close.rolling(50).mean().round(2),
        "volume_ratio": (volume / volume.rolling(20).mean()).round(2),
        "chg_5d_pct": ((close / close.shift(5) - 1) * 100).round(2),
        # controls, not part of the live screen:
        "mom_12_1": (close.shift(21) / close.shift(252) - 1) * 100,   # classic 12-1
        "mom_6_1": (close.shift(21) / close.shift(126) - 1) * 100,
        "lowvol": -close.pct_change().rolling(126).std() * 100,        # higher = calmer
    }


# --- screen: mirrors screen.profile_of + thesis.hard_gate_reason ------------

def qualified(ind: dict, close: pd.DataFrame, p: Params) -> pd.DataFrame:
    """Boolean [date x symbol]: bullish-qualified at this close."""
    ok = pd.DataFrame(True, index=close.index, columns=close.columns)
    ok &= close.notna() & ind["rsi_14"].notna()
    if p.min_volume_ratio > 0:
        vr = ind["volume_ratio"]
        ok &= vr.isna() | (vr >= p.min_volume_ratio)   # missing never blocks (live semantics)
    if p.max_move_5d_pct > 0:
        mv = ind["chg_5d_pct"]
        ok &= mv.isna() | (mv.abs() <= p.max_move_5d_pct)
    if p.max_rsi > 0:
        ok &= ind["rsi_14"] <= p.max_rsi
    ok &= ind["rsi_14"] >= p.min_rsi
    if p.require_trend:
        ok &= ind["sma_20"].notna() & ind["sma_50"].notna()
        ok &= (close > ind["sma_20"]) & (close > ind["sma_50"])
    return ok.fillna(False)


# --- portfolio simulation ---------------------------------------------------

def ranked_signals(qual: pd.DataFrame, metric: pd.DataFrame):
    """date -> [symbols], qualified that day, best metric first."""
    cols = np.array(qual.columns)
    qv, mv = qual.values, metric.values
    out = {}
    for i, d in enumerate(qual.index):
        idx = np.flatnonzero(qv[i])
        if idx.size == 0:
            out[d] = []
            continue
        m = np.nan_to_num(mv[i, idx], nan=-np.inf)
        out[d] = list(cols[idx[np.argsort(-m, kind="stable")]])  # stable: volume_ratio is rounded to 2dp, so ties are common
    return out


def run(frames: dict, ind: dict, p: Params, sectors: dict = None,
        start=None, end=None, capital: float = 10000.0):
    """Daily loop. Returns (trades DataFrame, equity Series).

    Ordering within a bar is deliberately conservative:
      1. exits are checked against THIS bar's low/close using the trail state
         carried in from the PREVIOUS bar -- so a position can never arm its
         trail and be stopped out by it on the same bar;
      2. the high-water mark is updated only after that;
      3. entries fill at this bar's open, from signals computed at the
         PREVIOUS bar's close (no look-ahead).
    """
    close, op, hi, lo = frames["close"], frames["open"], frames["high"], frames["low"]
    qual = qualified(ind, close, p)
    metric = ind.get(p.rank_by, ind["volume_ratio"])
    sigs = ranked_signals(qual, metric)

    dates = close.index
    if start is not None:
        dates = dates[dates >= pd.Timestamp(start)]
    if end is not None:
        dates = dates[dates <= pd.Timestamp(end)]
    dates = [d for d in dates if d >= close.index[60]]      # indicator warmup

    cost = p.cost_bps / 10000.0
    cash, pos, trades, curve = capital, {}, [], []
    prev = None

    for d in dates:
        o, h, l, c = op.loc[d], hi.loc[d], lo.loc[d], close.loc[d]

        # 1. exits (state as carried in from the previous bar)
        for sym in list(pos):
            t = pos[sym]
            px_l, px_c, px_o = l.get(sym), c.get(sym), o.get(sym)
            if pd.isna(px_c):
                continue
            e, reason, fill = t["entry"], None, None
            stop = e * (1 + p.disaster_stop_pct)
            if not pd.isna(px_l) and px_l <= stop:
                reason, fill = "disaster_stop", min(px_o, stop) if not pd.isna(px_o) else stop
            elif px_c <= t["invalidation"]:
                reason, fill = "invalidation", px_c               # close window only
            elif t["trailing"]:
                lvl = t["hwm"] * (1 + p.trail_stop_pct)
                if p.trail_floor_at_entry:
                    lvl = max(lvl, e)
                if not pd.isna(px_l) and px_l <= lvl:
                    reason, fill = "trailing_stop", min(px_o, lvl) if not pd.isna(px_o) else lvl
            if reason is None and t["held"] >= p.ttl_days:
                reason, fill = "ttl_expiry", px_c
            if reason:
                proceeds = t["shares"] * fill * (1 - cost)
                cash += proceeds
                trades.append(dict(symbol=sym, entry_date=t["date"], exit_date=d,
                                   entry=e, exit=fill, reason=reason, shares=t["shares"],
                                   held=t["held"], sector=(sectors or {}).get(sym),
                                   pnl=proceeds - t["cost_basis"],
                                   pnl_pct=(fill * (1 - cost)) / (e * (1 + cost)) - 1))
                del pos[sym]

        # 2. ratchet hwm / arm the trail on this bar
        for sym, t in pos.items():
            px_h = h.get(sym)
            if not pd.isna(px_h) and px_h > t["hwm"]:
                t["hwm"] = px_h
            if not t["trailing"] and t["hwm"] >= t["entry"] * (1 + p.trail_activate_pct):
                t["trailing"] = True
            t["held"] += 1

        # 3. entries at this open, from the previous close's screen
        if prev is not None and len(pos) < p.max_positions:
            held_sectors = [(sectors or {}).get(s) for s in pos]
            equity_now = cash + sum(t["shares"] * c.get(s, t["entry"]) for s, t in pos.items())
            for sym in sigs.get(prev, []):
                if len(pos) >= p.max_positions:
                    break
                if sym in pos:
                    continue
                sec = (sectors or {}).get(sym)
                if p.max_per_sector and sec and held_sectors.count(sec) >= p.max_per_sector:
                    continue
                px = o.get(sym)
                if pd.isna(px) or px <= 0:
                    continue
                budget = min(equity_now / p.max_positions, cash)
                if budget < 50:
                    continue
                shares = budget / (px * (1 + cost))
                basis = shares * px * (1 + cost)
                cash -= basis
                pos[sym] = dict(entry=px, date=d, shares=shares, cost_basis=basis,
                                hwm=px, trailing=False, held=0,
                                invalidation=px * (1 + p.invalidation_pct))
                held_sectors.append(sec)

        equity = cash + sum(t["shares"] * c.get(s, t["entry"]) for s, t in pos.items())
        curve.append((d, equity))
        prev = d

    return pd.DataFrame(trades), pd.Series(dict(curve)).sort_index()
