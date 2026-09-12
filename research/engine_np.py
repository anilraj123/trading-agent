"""Numpy-only port of engine.run -- same semantics, no pandas in the hot path.

TWO reasons, in order of importance:
  1. SPEED. engine.py does `op.loc[d]` then `.get(sym)` per symbol per day;
     that pandas overhead dominates a 2,688-iteration loop.
  2. The farm nodes have numpy but NOT pandas, so this is the only form of the
     engine that can be distributed -- the same split sweep/run.py uses.

`prepare()` does all the pandas work ONCE; `run_np()` touches arrays only.
research/verify_np.py proves this reproduces engine.run exactly.
"""
import numpy as np

from research.engine import Params, indicators, qualified


def prepare(frames, ind, sectors=None):
    """Dense arrays + integer sector ids. Pandas is used here and nowhere else."""
    close = frames["close"]
    syms = list(close.columns)
    q = qualified(ind, close, Params()).columns      # column order sanity
    assert list(q) == syms
    sec_names = sorted({(sectors or {}).get(s) for s in syms} - {None})
    sec_id = {n: i for i, n in enumerate(sec_names)}
    return {
        "dates": close.index.to_numpy(),
        "syms": np.array(syms),
        "open": frames["open"].to_numpy(np.float64),
        "high": frames["high"].to_numpy(np.float64),
        "low": frames["low"].to_numpy(np.float64),
        "close": close.to_numpy(np.float64),
        "ind": {k: v.to_numpy(np.float64) for k, v in ind.items()},
        "sector": np.array([sec_id.get((sectors or {}).get(s), -1) for s in syms]),
        "n_sectors": len(sec_names),
    }


def qualified_np(A, p: Params):
    ind, close = A["ind"], A["close"]
    rsi = ind["rsi_14"]
    ok = ~np.isnan(close) & ~np.isnan(rsi)
    if p.min_volume_ratio > 0:
        vr = ind["volume_ratio"]
        ok &= np.isnan(vr) | (vr >= p.min_volume_ratio)   # missing never blocks
    if p.max_move_5d_pct > 0:
        mv = ind["chg_5d_pct"]
        ok &= np.isnan(mv) | (np.abs(mv) <= p.max_move_5d_pct)
    with np.errstate(invalid="ignore"):
        if p.max_rsi > 0:
            ok &= rsi <= p.max_rsi
        ok &= rsi >= p.min_rsi
        if p.require_trend:
            s20, s50 = ind["sma_20"], ind["sma_50"]
            ok &= ~np.isnan(s20) & ~np.isnan(s50) & (close > s20) & (close > s50)
    return ok


def run_np(A, p: Params, start_i=60, end_i=None, capital=10000.0, membership=None):
    """Returns (trades list of dicts, equity np.ndarray, dates np.ndarray).

    `membership`: optional bool [date x symbol]; when given, a name may only be
    ENTERED on a bar where it was an index member. Positions already held are
    not force-sold on removal -- the exit rules still govern them -- which
    mirrors what a real portfolio does.
    """
    n_d, n_s = A["close"].shape
    end_i = n_d if end_i is None else min(end_i, n_d)
    o, h, l, c = A["open"], A["high"], A["low"], A["close"]
    qual = qualified_np(A, p)
    metric = A["ind"].get(p.rank_by, A["ind"]["volume_ratio"])
    sector, cost = A["sector"], p.cost_bps / 10000.0

    cash, curve, trades = capital, np.empty(end_i - start_i), []
    # open position state, parallel arrays indexed by symbol column
    held = np.zeros(n_s, bool)
    entry = np.zeros(n_s); shares = np.zeros(n_s); basis = np.zeros(n_s)
    hwm = np.zeros(n_s); trailing = np.zeros(n_s, bool)
    heldn = np.zeros(n_s, int); entered_at = np.zeros(n_s, int)

    for k, i in enumerate(range(start_i, end_i)):
        oi, hi_, li, ci = o[i], h[i], l[i], c[i]

        # 1. exits, on state carried in from the previous bar
        for j in np.flatnonzero(held):
            if np.isnan(ci[j]):
                continue
            e, reason, fill = entry[j], None, None
            stop = e * (1 + p.disaster_stop_pct)
            if not np.isnan(li[j]) and li[j] <= stop:
                reason = "disaster_stop"
                fill = min(oi[j], stop) if not np.isnan(oi[j]) else stop
            elif ci[j] <= e * (1 + p.invalidation_pct):
                reason, fill = "invalidation", ci[j]
            elif trailing[j]:
                lvl = hwm[j] * (1 + p.trail_stop_pct)
                if p.trail_floor_at_entry:
                    lvl = max(lvl, e)
                if not np.isnan(li[j]) and li[j] <= lvl:
                    reason = "trailing_stop"
                    fill = min(oi[j], lvl) if not np.isnan(oi[j]) else lvl
            if reason is None and heldn[j] >= p.ttl_days:
                reason, fill = "ttl_expiry", ci[j]
            if reason:
                proceeds = shares[j] * fill * (1 - cost)
                cash += proceeds
                trades.append(dict(symbol=A["syms"][j], entry_i=entered_at[j], exit_i=i,
                                   entry=e, exit=fill, reason=reason, shares=shares[j],
                                   held=heldn[j], pnl=proceeds - basis[j],
                                   pnl_pct=(fill * (1 - cost)) / (e * (1 + cost)) - 1))
                held[j] = False

        # 2. ratchet / arm
        for j in np.flatnonzero(held):
            if not np.isnan(hi_[j]) and hi_[j] > hwm[j]:
                hwm[j] = hi_[j]
            if not trailing[j] and hwm[j] >= entry[j] * (1 + p.trail_activate_pct):
                trailing[j] = True
            heldn[j] += 1

        # 3. entries at this open from the PREVIOUS bar's screen
        n_held = int(held.sum())
        # engine.run has `prev is None` on its first bar and so cannot enter
        # there; k == 0 is that same bar. Matching it keeps the two identical.
        if k > 0 and n_held < p.max_positions:
            elig = qual[i - 1].copy()
            if membership is not None:
                elig &= membership[i - 1]
            elig &= ~held
            cand = np.flatnonzero(elig)
            if cand.size:
                m = np.nan_to_num(metric[i - 1][cand], nan=-np.inf)
                cand = cand[np.argsort(-m, kind="stable")]   # see engine.ranked_signals
                pv = np.nansum(shares[held] * np.nan_to_num(ci[held], nan=0.0))
                equity_now = cash + pv
                sec_count = np.bincount(sector[held][sector[held] >= 0],
                                        minlength=A["n_sectors"]) if n_held else \
                            np.zeros(A["n_sectors"], int)
                for j in cand:
                    if n_held >= p.max_positions:
                        break
                    sj = sector[j]
                    if p.max_per_sector and sj >= 0 and sec_count[sj] >= p.max_per_sector:
                        continue
                    px = oi[j]
                    if np.isnan(px) or px <= 0:
                        continue
                    budget = min(equity_now / p.max_positions, cash)
                    if budget < 50:
                        continue
                    sh = budget / (px * (1 + cost))
                    basis[j] = sh * px * (1 + cost)
                    cash -= basis[j]
                    entry[j], shares[j], hwm[j] = px, sh, px
                    trailing[j], heldn[j], entered_at[j] = False, 0, i
                    held[j] = True
                    n_held += 1
                    if sj >= 0:
                        sec_count[sj] += 1

        curve[k] = cash + np.nansum(shares[held] * np.nan_to_num(ci[held], nan=0.0))

    return trades, curve, A["dates"][start_i:end_i]
