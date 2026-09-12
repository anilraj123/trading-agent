"""Performance metrics. Every backtest result goes through here so numbers
are comparable across runs."""
import numpy as np
import pandas as pd


def summarize(curve: pd.Series, trades: pd.DataFrame, bench: pd.Series = None) -> dict:
    if curve is None or len(curve) < 2:
        return {}
    r = curve.pct_change().dropna()
    yrs = max((curve.index[-1] - curve.index[0]).days / 365.25, 1e-9)
    total = curve.iloc[-1] / curve.iloc[0] - 1
    dd = (curve / curve.cummax() - 1)
    out = {
        "days": len(curve),
        "years": round(yrs, 2),
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(((1 + total) ** (1 / yrs) - 1) * 100, 2),
        "vol_pct": round(r.std() * np.sqrt(252) * 100, 2),
        "sharpe": round(r.mean() / r.std() * np.sqrt(252), 2) if r.std() > 0 else 0.0,
        "max_dd_pct": round(dd.min() * 100, 2),
        "time_in_mkt_pct": round((r != 0).mean() * 100, 1),
    }
    down = r[r < 0].std()
    out["sortino"] = round(r.mean() / down * np.sqrt(252), 2) if down and down > 0 else None
    if trades is not None and len(trades):
        w = trades[trades.pnl > 0]
        l = trades[trades.pnl <= 0]
        out.update({
            "n_trades": len(trades),
            "win_rate_pct": round(len(w) / len(trades) * 100, 1),
            "avg_win_pct": round(w.pnl_pct.mean() * 100, 2) if len(w) else 0.0,
            "avg_loss_pct": round(l.pnl_pct.mean() * 100, 2) if len(l) else 0.0,
            "avg_hold_days": round(trades.held.mean(), 1),
            "profit_factor": round(w.pnl.sum() / abs(l.pnl.sum()), 2) if len(l) and l.pnl.sum() else None,
            "expectancy_pct": round(trades.pnl_pct.mean() * 100, 3),
        })
    else:
        out["n_trades"] = 0
    if bench is not None and len(bench) > 1:
        b = bench.reindex(curve.index).ffill()
        bt = b.iloc[-1] / b.iloc[0] - 1
        out["bench_return_pct"] = round(bt * 100, 2)
        out["alpha_pct"] = round((total - bt) * 100, 2)
    return out


def exit_breakdown(trades: pd.DataFrame) -> pd.DataFrame:
    if trades is None or not len(trades):
        return pd.DataFrame()
    g = trades.groupby("reason").agg(n=("pnl", "size"), total=("pnl", "sum"),
                                     avg_pct=("pnl_pct", "mean"),
                                     win_rate=("pnl", lambda s: (s > 0).mean()))
    g["avg_pct"] = (g["avg_pct"] * 100).round(2)
    g["win_rate"] = (g["win_rate"] * 100).round(1)
    g["total"] = g["total"].round(2)
    return g.sort_values("total")
