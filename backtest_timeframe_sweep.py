"""1h vs Daily — controlled 180d window with cost model."""
import os, sys, warnings
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

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
WARMUP = 70
THRESHOLDS = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
LOOKBACK = 180
start_date = datetime.now() - timedelta(days=LOOKBACK)
end_date = datetime.now()

symbols = [
    "AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","JPM","V","JNJ",
    "WMT","PG","MA","UNH","HD","DIS","BAC","XOM","PFE","CSCO",
    "INTC","VZ","KO","PEP","MRK","ABT","TMO","COST","NFLX","ADBE",
    "CRM","AMD","QCOM","TXN","AVGO","ORCL","ACN","LLY","DHR","NKE",
    "NEE","BMY","UNP","LOW","PM","RTX","LIN","HON","AMGN","SPGI",
    "SPY","QQQ","IWM","DIA",
]

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

    for i in range(n):
        prices[i] = float(round(close.iloc[i], 2))
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
        s10 = sma_10.iloc[i]
        s20 = sma_20.iloc[i]
        trend_buy = 1.0 if (pd.notna(s10) and pd.notna(s20) and s10 > s20) else 0.0
        trend_sell = 1.0 if (pd.notna(s10) and pd.notna(s20) and s10 < s20) else 0.0
        m5 = mom5.iloc[i] if pd.notna(mom5.iloc[i]) else 0.0
        mom_b = min(1.0, (m5 - mom_thresh) / mom_thresh) * mom_w if m5 > mom_thresh else 0.0
        mom_s = min(1.0, (abs(m5) - mom_thresh) / mom_thresh) * mom_w if m5 < -mom_thresh else 0.0
        vr = vol_ratio.iloc[i] if pd.notna(vol_ratio.iloc[i]) else 1.0
        boost = vol_boost_val if vr > vol_thresh else 1.0
        curr_v = volume.iloc[i]
        bs = (macd_buy * macd_w + trend_buy * trend_w + mom_b) * boost
        ss = (rsi_sell + macd_sell * macd_w + bb_sell + trend_sell * trend_w + mom_s) * boost
        if pd.notna(r) and r > 80:
            bs = 0.0
        if curr_v < 1000:
            bs = 0.0
        buy_scores[i] = float(round(bs, 2))
        sell_scores[i] = float(round(ss, 2))
    return prices, buy_scores, sell_scores, sym_df.index

def run_backtest(data, all_times, threshold, warmup):
    position = None
    trades = []
    cap = INITIAL_CAPITAL
    sell_min = Config.TA_MIN_SELL_SCORE
    for ts in all_times:
        if position is not None:
            sym = position["sym"]
            entry = data.get(sym)
            if entry is None: continue
            i = entry["index"].get_indexer([ts])[0]
            if i < 0: continue
            price = entry["prices"][i]
            ss = entry["sell_scores"][i]
            stop = position["entry_price"] * (1 + Config.TA_STOP_LOSS_PCT)
            if ss >= sell_min or price <= stop:
                exit_p = stop if price <= stop else price
                pnl = (exit_p - position["entry_price"]) / position["entry_price"]
                net_pnl = ((exit_p * (1 - COST_BPS)) - (position["entry_price"] * (1 + COST_BPS))) / position["entry_price"]
                cap *= 1 + net_pnl
                trades.append({"sym":sym,"pnl_pct":round(pnl*100,2),"net_pnl_pct":round(net_pnl*100,2),"win":net_pnl>0,"buy_score":position["buy_score"],"sell_score":ss,"hit_stop":price<=stop})
                position = None
        if position is None:
            candidates = []
            for sym, entry in data.items():
                i = entry["index"].get_indexer([ts])[0]
                if i < 0: continue
                if i < warmup: continue
                bs = entry["buy_scores"][i]
                if bs < threshold: continue
                candidates.append((sym, entry["prices"][i], bs))
            if candidates:
                best = max(candidates, key=lambda x: x[2])
                position = {"sym":best[0],"entry_price":best[1]*(1+COST_BPS),"buy_score":best[2]}
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
    sharpe = np.mean(rets) / np.std(rets) if len(rets)>1 and np.std(rets)>0 else 0
    avg_ret = np.mean(rets) if rets else 0
    return {"trades":len(trades),"win_rate":len(wins)/len(trades)*100 if trades else 0,"avg_win":avg_win,"avg_loss":avg_loss,"avg_ret":avg_ret,"gross_return":total_return,"max_dd":max_dd,"sharpe":round(sharpe,2)}

for tf_name, tf in [("1h", TimeFrame(1, TimeFrameUnit.Hour)), ("Daily", TimeFrame(1, TimeFrameUnit.Day))]:
    print(f"--- {tf_name} (last {LOOKBACK}d, cost {COST_BPS*2*10000:.0f}bps rt) ---")
    req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=tf, start=start_date, end=end_date)
    try:
        bars = client.get_stock_bars(req)
    except Exception as e:
        print(f"FAILED: {e}"); continue
    df = bars.df
    data = {}; all_times = set()
    for sym in symbols:
        if sym not in df.index.get_level_values("symbol"): continue
        sym_df = df.xs(sym, level="symbol").copy()
        if len(sym_df) <= WARMUP + 10: continue
        all_times.update(sym_df.index.tolist())
        prices, bss, sss, idx = precompute(sym_df)
        data[sym] = {"prices":prices,"buy_scores":bss,"sell_scores":sss,"index":idx}
    all_times = sorted(all_times)
    print(f"  {len(data)} syms, {len(all_times)} bars")
    best_t = None; best_sharpe = -999; best_m = None
    for t in THRESHOLDS:
        cap, trades = run_backtest(data, all_times, t, WARMUP)
        m = metrics(trades, cap)
        if m["sharpe"] > best_sharpe and m["trades"] >= 20:
            best_sharpe = m["sharpe"]; best_t = t; best_m = m
    if best_m:
        m = best_m
        print(f"  Best thresh={best_t}  Sharpe={m['sharpe']}")
        print(f"  Trades={m['trades']:>4}  Win%={m['win_rate']:>5.1f}  "
              f"AvgRet={m['avg_ret']:>+7.2f}%  "
              f"AvgWin={m['avg_win']:>+7.2f}%  AvgLoss={m['avg_loss']:>+7.2f}%  "
              f"Return={m['gross_return']:>+8.2f}%  MaxDD={m['max_dd']:>5.1f}%  "
              f"Sharpe={m['sharpe']:>5.2f}")
        print(f"  Sweep:", end="")
        for t in THRESHOLDS:
            cap, trades = run_backtest(data, all_times, t, WARMUP)
            m2 = metrics(trades, cap)
            print(f"  {t}: R={m2['gross_return']:+.1f}% S={m2['sharpe']:.2f} T={m2['trades']}", end="")
        print()
    else:
        print("  No threshold met min 20 trades")
    print()
