"""Walk-forward with SPY > 200d SMA regime filter — 50 individual stocks."""
import os, sys, warnings
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()
from alpaca.data import StockHistoricalDataClient, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from trader.config import Config

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

INITIAL_CAPITAL = 10000
COST_BPS = 0.0005
WARMUP = 30

symbols = [
    "AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","JPM","V","JNJ",
    "WMT","PG","MA","UNH","HD","DIS","BAC","XOM","PFE","CSCO",
    "INTC","VZ","KO","PEP","MRK","ABT","TMO","COST","NFLX","ADBE",
    "CRM","AMD","QCOM","TXN","AVGO","ORCL","ACN","LLY","DHR","NKE",
    "NEE","BMY","UNP","LOW","PM","RTX","LIN","HON","AMGN","SPGI",
]
# Add SPY for regime filter only (won't be traded)
ALL_SYMS = symbols + ["SPY"]

def precompute(sym_df):
    close = sym_df["close"]; volume = sym_df["volume"]; n = len(sym_df)
    delta = close.diff()
    gain = delta.where(delta>0,0).rolling(14).mean(); loss = (-delta.where(delta<0,0)).rolling(14).mean()
    rs = gain/loss.replace(0,np.nan); rsi = 100-(100/(1+rs))
    ema8 = close.ewm(span=8,adjust=False).mean(); ema21 = close.ewm(span=21,adjust=False).mean()
    macd_l = ema8-ema21; macd_s = macd_l.ewm(span=5,adjust=False).mean(); macd_h = macd_l-macd_s
    sma10 = close.rolling(10).mean(); sma20 = close.rolling(20).mean()
    bb_m = close.rolling(20).mean(); bb_s = close.rolling(20).std()
    bb_pos = (close-(bb_m-2*bb_s))/((bb_m+2*bb_s)-(bb_m-2*bb_s)).replace(0,np.nan)
    vol_avg = volume.rolling(20).mean(); vol_r = volume/vol_avg.replace(0,np.nan)
    mom5 = close.pct_change(5)*100
    bss = np.zeros(n); sss = np.zeros(n); pr = np.zeros(n)
    for i in range(n):
        pr[i] = round(close.iloc[i],2)
        if i < WARMUP: continue
        r = rsi.iloc[i]; mh = macd_h.iloc[i]; mt = "bullish" if macd_l.iloc[i]>macd_s.iloc[i] else "bearish"
        s10 = sma10.iloc[i]; s20 = sma20.iloc[i]
        rsi_sell = min(1.0,(r-Config.TA_RSI_OVERBOUGHT)/(100-Config.TA_RSI_OVERBOUGHT))*Config.TA_RSI_WEIGHT if pd.notna(r) and r>Config.TA_RSI_OVERBOUGHT else 0.0
        macd_buy = 1.0 if pd.notna(mh) and mh>0 and mt=="bullish" else 0.0; macd_sell = 1.0 if pd.notna(mh) and mh<0 and mt=="bearish" else 0.0
        bp = bb_pos.iloc[i]
        bb_sell = min(1.0,(bp-Config.TA_BB_UPPER_THRESHOLD)/(1-Config.TA_BB_UPPER_THRESHOLD))*Config.TA_BB_WEIGHT if pd.notna(bp) and bp>Config.TA_BB_UPPER_THRESHOLD else 0.0
        tr_buy = 1.0 if pd.notna(s10) and pd.notna(s20) and s10>s20 else 0.0; tr_sell = 1.0 if pd.notna(s10) and pd.notna(s20) and s10<s20 else 0.0
        m5 = mom5.iloc[i] if pd.notna(mom5.iloc[i]) else 0.0
        mom_b = min(1.0,(m5-Config.TA_MOM_THRESHOLD)/Config.TA_MOM_THRESHOLD)*Config.TA_MOM_WEIGHT if m5>Config.TA_MOM_THRESHOLD else 0.0
        mom_s = min(1.0,(abs(m5)-Config.TA_MOM_THRESHOLD)/Config.TA_MOM_THRESHOLD)*Config.TA_MOM_WEIGHT if m5<-Config.TA_MOM_THRESHOLD else 0.0
        vr = vol_r.iloc[i] if pd.notna(vol_r.iloc[i]) else 1.0
        boost = Config.TA_VOL_BOOST if vr>Config.TA_VOL_THRESHOLD else 1.0
        bs = (macd_buy*Config.TA_MACD_WEIGHT + tr_buy*Config.TA_TREND_WEIGHT + mom_b)*boost
        ss_ = (rsi_sell + macd_sell*Config.TA_MACD_WEIGHT + bb_sell + tr_sell*Config.TA_TREND_WEIGHT + mom_s)*boost
        if pd.notna(r) and r>80: bs=0.0
        if volume.iloc[i]<1000: bs=0.0
        bss[i]=round(bs,2); sss[i]=round(ss_,2)
    return pr, bss, sss, close

# Fetch
end = datetime.now()
start = end - timedelta(days=365)
print("Fetching 365d daily bars...")
req = StockBarsRequest(symbol_or_symbols=ALL_SYMS, timeframe=TimeFrame(1, TimeFrameUnit.Day), start=start, end=end)
bars = client.get_stock_bars(req)
df = bars.df

data = {}
spy_sma200_array = None
spy_index = None
all_times = set()

for sym in ALL_SYMS:
    if sym not in df.index.get_level_values("symbol"): continue
    sym_df = df.xs(sym, level="symbol").copy()
    if len(sym_df) <= WARMUP+10: continue
    for t in sym_df.index: all_times.add(t.tz_localize(None))
    if sym == "SPY":
        spy_index = sym_df.index.tz_localize(None)
        spy_sma200 = sym_df["close"].rolling(200).mean()
        spy_sma200_array = spy_sma200.values
    else:
        pr, bss, sss, _ = precompute(sym_df)
        data[sym] = {"prices":pr,"buy_scores":bss,"sell_scores":sss,"index":sym_df.index.tz_localize(None)}

all_times = sorted(all_times)
print(f"Loaded {len(data)} symbols + SPY, {len(all_times)} bars\n")

# 3 contiguous windows
n = len(all_times); split1 = n // 3; split2 = 2 * n // 3
windows = [
    ("W1 (earliest)", all_times[:split1]),
    ("W2 (middle)",   all_times[split1:split2]),
    ("W3 (latest)",   all_times[split2:]),
]

THRESHOLD = 1.0

def backtest(times, use_filter):
    pos = None; trades = []; cap = INITIAL_CAPITAL
    for ts in times:
        # --- Regime filter ---
        if use_filter and pos is None and spy_index is not None:
            si = spy_index.get_indexer([ts])[0]
            if si >= 200:  # need 200 bars for SMA200
                spy_c = data.get("_spy_prices", None)
                # Lazy-load SPY prices
                pass
            # Actually, compute on the fly
            spy_close_series = df.xs("SPY", level="symbol")["close"]
            spy_sma200_s = spy_close_series.rolling(200).mean()
            spy_idx_naive = spy_close_series.index.tz_localize(None)
            si2 = spy_idx_naive.get_indexer([ts])[0]
            if si2 >= 0 and si2 < len(spy_close_series):
                if pd.isna(spy_sma200_s.iloc[si2]) or spy_close_series.iloc[si2] <= spy_sma200_s.iloc[si2]:
                    continue  # skip — market not in uptrend

        if pos is not None:
            e = data.get(pos["sym"])
            if e is None: continue
            i = e["index"].get_indexer([ts])[0]
            if i < 0: continue
            pr = e["prices"][i]; ss = e["sell_scores"][i]
            stop = pos["entry_price"] * (1 + Config.TA_STOP_LOSS_PCT)
            if ss >= Config.TA_MIN_SELL_SCORE or pr <= stop:
                ex = stop if pr <= stop else pr
                net = ((ex*(1-COST_BPS))-(pos["entry_price"]*(1+COST_BPS)))/pos["entry_price"]
                cap *= 1 + net; trades.append({"win":net>0,"net":round(net*100,2)})
                pos = None
        if pos is None:
            cand = []
            for sym, e in data.items():
                i = e["index"].get_indexer([ts])[0]
                if i < 0 or i < WARMUP: continue
                bs = e["buy_scores"][i]
                if bs < THRESHOLD: continue
                cand.append((sym, e["prices"][i], bs))
            if cand:
                b = max(cand, key=lambda x: x[2])
                pos = {"sym":b[0], "entry_price":b[1]*(1+COST_BPS)}
    return cap, trades

def metrics(times, use_filter):
    cap, trades = backtest(times, use_filter)
    ret = (cap/INITIAL_CAPITAL-1)*100
    wins = [t for t in trades if t["win"]]; losses = [t for t in trades if not t["win"]]
    wr = len(wins)/len(trades)*100 if trades else 0
    aw = np.mean([t["net"] for t in wins]) if wins else 0
    al = np.mean([t["net"] for t in losses]) if losses else 0
    rets_arr = [t["net"] for t in trades]
    sharpe = np.mean(rets_arr)/np.std(rets_arr) if len(rets_arr)>1 and np.std(rets_arr)>0 else 0
    peak = INITIAL_CAPITAL; running = INITIAL_CAPITAL; mdd = 0
    for t in trades:
        running *= 1+t["net"]/100
        if running > peak: peak = running
        dd = (peak-running)/peak*100
        if dd > mdd: mdd = dd
    payoff = abs(aw/al) if al != 0 else 0
    return {"trades":len(trades),"win_rate":wr,"avg_win":aw,"avg_loss":al,"payoff":payoff,"gross_return":ret,"max_dd":mdd,"sharpe":sharpe}

# --- No filter ---
print("=== NO MARKET FILTER ===")
print(f"{'Window':<15} {'Trades':>7} {'Win%':>7} {'AvgWin':>8} {'AvgLoss':>8} {'Payoff':>7} {'Return':>9} {'MaxDD':>7} {'Sharpe':>7}")
print("-" * 75)
for wname, times in windows:
    m = metrics(times, False)
    print(f"{wname:<15} {m['trades']:>7} {m['win_rate']:>6.1f}% {m['avg_win']:>+7.2f}% {m['avg_loss']:>+7.2f}% {m['payoff']:>6.2f} {m['gross_return']:>+8.2f}% {m['max_dd']:>6.1f}% {m['sharpe']:>6.2f}")
m = metrics(all_times, False)
print(f"{'Full':<15} {m['trades']:>7} {m['win_rate']:>6.1f}% {m['avg_win']:>+7.2f}% {m['avg_loss']:>+7.2f}% {m['payoff']:>6.2f} {m['gross_return']:>+8.2f}%")
print()

# --- With SPY 200d SMA filter ---
print("=== SPY > 200d SMA FILTER ===")
print(f"{'Window':<15} {'Trades':>7} {'Win%':>7} {'AvgWin':>8} {'AvgLoss':>8} {'Payoff':>7} {'Return':>9} {'MaxDD':>7} {'Sharpe':>7}")
print("-" * 75)
for wname, times in windows:
    m = metrics(times, True)
    print(f"{wname:<15} {m['trades']:>7} {m['win_rate']:>6.1f}% {m['avg_win']:>+7.2f}% {m['avg_loss']:>+7.2f}% {m['payoff']:>6.2f} {m['gross_return']:>+8.2f}% {m['max_dd']:>6.1f}% {m['sharpe']:>6.2f}")
m = metrics(all_times, True)
print(f"{'Full':<15} {m['trades']:>7} {m['win_rate']:>6.1f}% {m['avg_win']:>+7.2f}% {m['avg_loss']:>+7.2f}% {m['payoff']:>6.2f} {m['gross_return']:>+8.2f}%")
