"""Daily walk-forward with SPY > 200d SMA regime filter + trailing ATR stop."""

import os, sys, warnings
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from trader.config import Config

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

INITIAL_CAPITAL = 10000
COST_BPS = 0.0005

symbols = [
    "AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","JPM","V","JNJ",
    "WMT","PG","MA","UNH","HD","DIS","BAC","XOM","PFE","CSCO",
    "INTC","VZ","KO","PEP","MRK","ABT","TMO","COST","NFLX","ADBE",
    "CRM","AMD","QCOM","TXN","AVGO","ORCL","ACN","LLY","DHR","NKE",
    "NEE","BMY","UNP","LOW","PM","RTX","LIN","HON","AMGN","SPGI",
    "SPY","QQQ","IWM","DIA",
]

WARMUP = 30

def precompute(sym_df):
    close = sym_df["close"]
    volume = sym_df["volume"]
    n = len(sym_df)

    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi_vals = 100 - (100 / (1 + rs))

    ema_fast = close.ewm(span=8, adjust=False).mean()
    ema_slow = close.ewm(span=21, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    macd_signal = macd_line.ewm(span=5, adjust=False).mean()
    macd_hist = macd_line - macd_signal

    sma_10 = close.rolling(10).mean()
    sma_20 = close.rolling(20).mean()
    bb_sma = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bb_upper = bb_sma + 2 * bb_std
    bb_lower = bb_sma - 2 * bb_std
    bb_pos = (close - bb_lower) / (bb_upper - bb_lower).replace(0, np.nan)
    vol_avg = volume.rolling(20).mean()
    vol_ratio = volume / vol_avg.replace(0, np.nan)
    mom5 = close.pct_change(periods=5) * 100

    # ATR(14) for trailing stop
    prev_close = close.shift(1)
    tr1 = sym_df["high"] - sym_df["low"]
    tr2 = (sym_df["high"] - prev_close).abs()
    tr3 = (sym_df["low"] - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean()

    rsi_ob = Config.TA_RSI_OVERBOUGHT
    rsi_w = Config.TA_RSI_WEIGHT
    macd_w = Config.TA_MACD_WEIGHT
    bb_w = Config.TA_BB_WEIGHT
    bb_upper_thresh = Config.TA_BB_UPPER_THRESHOLD
    trend_w = Config.TA_TREND_WEIGHT
    mom_w = Config.TA_MOM_WEIGHT
    mom_thresh = Config.TA_MOM_THRESHOLD
    vol_thresh = Config.TA_VOL_THRESHOLD
    vol_boost_val = Config.TA_VOL_BOOST

    buy_scores = np.zeros(n)
    sell_scores = np.zeros(n)
    prices = np.zeros(n)
    atr_arr = np.zeros(n)

    for i in range(n):
        prices[i] = float(round(close.iloc[i], 2))
        atr_arr[i] = atr14.iloc[i] if pd.notna(atr14.iloc[i]) else 0
        if i < WARMUP:
            continue
        r = rsi_vals.iloc[i]
        rsi_sell = min(1.0, (r - rsi_ob) / (100 - rsi_ob)) * rsi_w if pd.notna(r) and r > rsi_ob else 0.0
        mh = macd_hist.iloc[i]
        mt = "bullish" if macd_line.iloc[i] > macd_signal.iloc[i] else "bearish"
        macd_buy = 1.0 if (pd.notna(mh) and mh > 0 and mt == "bullish") else 0.0
        macd_sell = 1.0 if (pd.notna(mh) and mh < 0 and mt == "bearish") else 0.0
        bp = bb_pos.iloc[i]
        bb_sell = min(1.0, (bp - bb_upper_thresh) / (1 - bb_upper_thresh)) * bb_w if pd.notna(bp) and bp > bb_upper_thresh else 0.0
        s10 = sma_10.iloc[i]; s20 = sma_20.iloc[i]
        trend_buy = 1.0 if (pd.notna(s10) and pd.notna(s20) and s10 > s20) else 0.0
        trend_sell = 1.0 if (pd.notna(s10) and pd.notna(s20) and s10 < s20) else 0.0
        m5 = mom5.iloc[i] if pd.notna(mom5.iloc[i]) else 0.0
        mom_b = min(1.0, (m5 - mom_thresh) / mom_thresh) * mom_w if m5 > mom_thresh else 0.0
        mom_s = min(1.0, (abs(m5) - mom_thresh) / mom_thresh) * mom_w if m5 < -mom_thresh else 0.0
        vr = vol_ratio.iloc[i] if pd.notna(vol_ratio.iloc[i]) else 1.0
        boost = vol_boost_val if vr > vol_thresh else 1.0
        bs = (macd_buy * macd_w + trend_buy * trend_w + mom_b) * boost
        ss = (rsi_sell + macd_sell * macd_w + bb_sell + trend_sell * trend_w + mom_s) * boost
        if pd.notna(r) and r > 80: bs = 0.0
        if volume.iloc[i] < 1000: bs = 0.0
        buy_scores[i] = float(round(bs, 2))
        sell_scores[i] = float(round(ss, 2))
    return prices, buy_scores, sell_scores, atr_arr, sym_df.index


def run_backtest(data, all_times, threshold, warmup, use_regime_filter, trailing_atr_mult):
    position = None
    trades = []
    cap = INITIAL_CAPITAL
    sell_min = Config.TA_MIN_SELL_SCORE

    for ts in all_times:
        # --- Regime filter: only trade if SPY above 200d SMA ---
        if use_regime_filter and position is None:
            spy_entry = data.get("SPY")
            if spy_entry:
                si = spy_entry["index"].get_indexer([ts])
                if si[0] >= warmup:
                    spy_close = spy_entry["prices"][si[0]]
                    spy_sma200 = spy_entry.get("sma200", None)
                    spy_sma = spy_sma200[si[0]] if spy_sma200 is not None else np.nan
                    if pd.isna(spy_sma) or spy_close <= spy_sma:
                        continue  # skip this bar, market not in uptrend

        if position is not None:
            sym = position["sym"]
            entry = data.get(sym)
            if entry is None: continue
            i = entry["index"].get_indexer([ts])[0]
            if i < 0: continue
            price = entry["prices"][i]
            ss = entry["sell_scores"][i]

            # Trailing stop (ATR-based)
            atr_v = entry["atr"][i]
            if trailing_atr_mult > 0 and atr_v > 0:
                trail_stop = position["peak_price"] - (trailing_atr_mult * atr_v)
            else:
                trail_stop = position["entry_price"] * (1 + Config.TA_STOP_LOSS_PCT)

            # Update peak
            if price > position["peak_price"]:
                position["peak_price"] = price

            exit_p = None
            if price <= trail_stop:
                exit_p = trail_stop if False else price  # exit at bar's close price on stop
                hit_stop = True
            elif ss >= sell_min:
                exit_p = price
                hit_stop = False

            if exit_p is not None:
                pnl = (exit_p - position["entry_price"]) / position["entry_price"]
                net_pnl = ((exit_p*(1-COST_BPS)) - (position["entry_price"]*(1+COST_BPS))) / position["entry_price"]
                cap *= 1 + net_pnl
                trades.append({"sym":sym,"pnl_pct":round(pnl*100,2),"net_pnl_pct":round(net_pnl*100,2),"win":net_pnl>0,"hit_stop":hit_stop})
                position = None

        if position is None:
            candidates = []
            for sym, entry in data.items():
                if sym == "SPY": continue
                i = entry["index"].get_indexer([ts])[0]
                if i < 0 or i < warmup: continue
                bs = entry["buy_scores"][i]
                if bs < threshold: continue
                candidates.append((sym, entry["prices"][i], bs, entry["atr"][i]))
            if candidates:
                best = max(candidates, key=lambda x: x[2])
                entry_cost = best[1] * (1 + COST_BPS)
                position = {
                    "sym": best[0],
                    "entry_price": entry_cost,
                    "peak_price": entry_cost,
                    "buy_score": best[2],
                }
    return cap, trades


def metrics(trades, final_cap):
    total_return = ((final_cap / INITIAL_CAPITAL) - 1) * 100
    wins = [x for x in trades if x["win"]]
    losses = [x for x in trades if not x["win"]]
    avg_win = np.mean([x["net_pnl_pct"] for x in wins]) if wins else 0
    avg_loss = np.mean([x["net_pnl_pct"] for x in losses]) if losses else 0
    max_dd = 0; peak = INITIAL_CAPITAL; running = INITIAL_CAPITAL
    for x in trades:
        running *= 1 + x["net_pnl_pct"] / 100
        if running > peak: peak = running
        dd = (peak - running) / peak * 100
        if dd > max_dd: max_dd = dd
    rets = [x["net_pnl_pct"] for x in trades]
    sharpe = np.mean(rets)/np.std(rets) if len(rets)>1 and np.std(rets)>0 else 0
    payoff = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    return {"trades":len(trades),"win_rate":len(wins)/len(trades)*100 if trades else 0,"avg_win":avg_win,"avg_loss":avg_loss,"payoff":payoff,"gross_return":total_return,"max_dd":max_dd,"sharpe":round(sharpe,2)}


# --- Fetch data ---
end = datetime.now()
start_all = end - timedelta(days=365)

print("Fetching daily bars (365d lookback)...")
req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame(1, TimeFrameUnit.Day), start=start_all, end=end)
bars = client.get_stock_bars(req)
df = bars.df

# Pre-compute once
full_data = {}
all_full_times = set()
for sym in symbols:
    if sym not in df.index.get_level_values("symbol"): continue
    sym_df = df.xs(sym, level="symbol").copy()
    if len(sym_df) <= WARMUP + 20: continue
    all_full_times.update(t.tz_localize(None) for t in sym_df.index)
    prices, bss, sss, atr_arr, idx = precompute(sym_df)
    entry = {"prices": prices, "buy_scores": bss, "sell_scores": sss, "atr": atr_arr, "index": idx}
    # Compute SPY sma200
    if sym == "SPY":
        spy_close = sym_df["close"]
        spy_sma200 = spy_close.rolling(200).mean()
        spy_sma200_arr = np.full(len(sym_df), np.nan)
        for i in range(len(sym_df)):
            spy_sma200_arr[i] = spy_sma200.iloc[i] if pd.notna(spy_sma200.iloc[i]) else np.nan
        entry["sma200"] = spy_sma200_arr
    # Make index naive for consistent comparison
    entry["index"] = entry["index"].tz_localize(None)
    full_data[sym] = entry

all_full_times = sorted(all_full_times)
print(f"Loaded {len(full_data)} symbols, {len(all_full_times)} bars\n")

# --- Walk-forward windows ---
now = datetime.now()
windows = [
    ("W1 (now-120d)", now - pd.Timedelta(120, "D"), now),
    ("W2 (60-180d)",  now - pd.Timedelta(180, "D"), now - pd.Timedelta(60, "D")),
    ("W3 (120-240d)", now - pd.Timedelta(240, "D"), now - pd.Timedelta(120, "D")),
]

THRESHOLD = 1.0  # fixed after WF showed it barely matters
VARIANTS = [
    ("No filter, fixed stop",       False, 0),
    ("Regime filter, fixed stop",   True,  0),
    ("No filter, trailing 3 ATR",   False, 3),
    ("Regime + trailing 3 ATR",     True,  3),
]

print(f"{'Variant':<32} {'Window':<18} {'Trades':>6} {'Win%':>6} {'AvgWin':>8} {'AvgLoss':>8} {'Payoff':>7} {'Return':>8} {'MaxDD':>6} {'Sharpe':>6}")
print("-" * 105)

for vname, use_filter, trail_mult in VARIANTS:
    for wname, wstart, wend in windows:
        win_times = [t for t in all_full_times if wstart <= t <= wend]
        cap, trades = run_backtest(full_data, win_times, THRESHOLD, WARMUP, use_filter, trail_mult)
        m = metrics(trades, cap)
        print(f"{vname:<32} {wname:<18} {m['trades']:>6} {m['win_rate']:>5.1f}% "
              f"{m['avg_win']:>+7.2f}% {m['avg_loss']:>+7.2f}% {m['payoff']:>6.2f} "
              f"{m['gross_return']:>+7.2f}% {m['max_dd']:>5.1f}% {m['sharpe']:>5.2f}")
    print()
