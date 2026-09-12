#!/usr/bin/env python3
"""Stage 2: run ONE parameter config against the cached precompute, print JSON.

numpy only - no pandas, no alpaca, no network, no .env. That is what lets this
run on the laptops, which have python3 + numpy and nothing else.

  ./run.py --cache cache.npz --min-buy 1.5 --min-sell 1.0 --stop-loss -0.03
"""
import argparse, json, os, numpy as np

_C = {}


def load(path):
    if path not in _C:
        z = np.load(path, allow_pickle=True)
        _C[path] = dict(priceM=z["priceM"], sellM=z["sellM"], buyM=z["buyM"],
                        hasbar=z["hasbar"], symbols=[str(s) for s in z["symbols"]],
                        cap0=float(z["initial_capital"]))
    return _C[path]


def simulate(c, min_buy, min_sell, stop_loss, lo=0, hi=None, cost=0.0):
    """Faithful replay of the original run(), with the per-symbol get_indexer scan
    replaced by one vectorised row lookup. np.argmax returns the FIRST maximum and
    the columns are in the original's dict-insertion order, so ties break the same
    way max(candidates, key=score) broke them.

    `cost` is a one-way fraction (5 bps -> 0.0005), charged on entry AND exit. It is
    applied to P&L only, never to the stop level or the entry/exit signal, so the
    trade SEQUENCE is identical at every cost level and cost=0 reproduces the
    original exactly. Note backtest_daily_walkforward.py charges the entry side
    twice - once building entry_price (l.184), again inside net_pnl (l.168); this
    charges each side once."""
    priceM, sellM, buyM, hasbar = c["priceM"], c["sellM"], c["buyM"], c["hasbar"]
    T = priceM.shape[0] if hi is None else hi
    cap = c["cap0"]
    held = None            # (col, entry_price, entry_buy_score)
    trades = []

    for t in range(lo, T):        # a fold starts flat, like run_backtest on a time subset
        if held is not None:
            s, entry_px, entry_bs = held
            if not hasbar[t, s]:
                continue                       # original 'continue' skips the buy block too
            price = priceM[t, s]
            ss = sellM[t, s]
            stop = entry_px * (1 + stop_loss)
            if ss >= min_sell or price <= stop:
                exit_px = stop if price <= stop else price
                eff_in = entry_px * (1 + cost)      # pay the spread getting in ...
                eff_out = exit_px * (1 - cost)      # ... and again getting out
                pnl = (eff_out - eff_in) / eff_in
                cap *= 1 + pnl
                gross = (exit_px - entry_px) / entry_px
                trades.append({"sym": c["symbols"][s], "pnl_pct": round(pnl * 100, 2),
                               "gross_pnl_pct": round(gross * 100, 2),
                               "win": pnl > 0, "buy_score": entry_bs,
                               "sell_score": float(ss), "hit_stop": bool(price <= stop)})
                held = None

        if held is None:
            row = buyM[t]
            j = int(np.argmax(row))
            if row[j] >= min_buy:
                held = (j, priceM[t, j], float(row[j]))

    return cap, trades


def metrics(cap, trades, cap0):
    wins = [x for x in trades if x["win"]]
    losses = [x for x in trades if not x["win"]]
    max_dd, peak, running = 0.0, cap0, cap0
    for x in trades:
        running *= 1 + x["pnl_pct"] / 100
        peak = max(peak, running)
        max_dd = max(max_dd, (peak - running) / peak * 100)
    rets = [x["pnl_pct"] for x in trades]
    sharpe = (np.mean(rets) / np.std(rets)) if rets and np.std(rets) > 0 else 0.0
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0.0,
        "avg_win": float(np.mean([x["pnl_pct"] for x in wins])) if wins else 0.0,
        "avg_loss": float(np.mean([x["pnl_pct"] for x in losses])) if losses else 0.0,
        "gross_return": ((cap / cap0) - 1) * 100,
        "max_dd": max_dd,
        "sharpe": float(sharpe),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cache", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.npz"))
    p.add_argument("--min-buy", type=float)
    p.add_argument("--configs", help="batch: 'buy,sell,stop;buy,sell,stop;...' - one JSON line each. "
                                     "A config costs ~0.1s but SSH costs ~0.5s, so tasks MUST batch.")
    p.add_argument("--min-sell", type=float, default=1.0)
    p.add_argument("--stop-loss", type=float, default=-0.03)
    p.add_argument("--cost-bps", type=float, default=0.0,
                   help="one-way cost in BASIS POINTS (5 = 5bps = 0.0005), charged on entry and exit")
    a = p.parse_args()

    c = load(a.cache)
    if a.configs:
        for chunk in a.configs.split(";"):
            if not chunk.strip():
                continue
            mb, ms, sl = (float(x) for x in chunk.split(","))
            cap, trades = simulate(c, mb, ms, sl, cost=a.cost_bps / 1e4)
            r = metrics(cap, trades, c["cap0"])
            r.update(min_buy=mb, min_sell=ms, stop_loss=sl, cost_bps=a.cost_bps)
            print(json.dumps(r), flush=True)
        return
    if a.min_buy is None:
        p.error("need --min-buy or --configs")
    cap, trades = simulate(c, a.min_buy, a.min_sell, a.stop_loss, cost=a.cost_bps / 1e4)
    r = metrics(cap, trades, c["cap0"])
    r.update(min_buy=a.min_buy, min_sell=a.min_sell, stop_loss=a.stop_loss, cost_bps=a.cost_bps)
    print(json.dumps(r))


if __name__ == "__main__":
    main()
