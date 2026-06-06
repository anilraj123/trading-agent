"""5-minute bar backtest sweep — vectorized TA pre-computation."""

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
from trader.technical_analysis import TechnicalAnalysis

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

INITIAL_CAPITAL = 10000
WARMUP = 60
DAYS = 20

symbols = [
    "AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","JPM","V","JNJ",
    "WMT","PG","MA","UNH","HD","DIS","BAC","XOM","PFE","CSCO",
    "INTC","VZ","KO","PEP","MRK","ABT","TMO","COST","NFLX","ADBE",
    "CRM","AMD","QCOM","TXN","AVGO","ORCL","ACN","LLY","DHR","NKE",
    "NEE","BMY","UNP","LOW","PM","RTX","LIN","HON","AMGN","SPGI",
    "SPY","QQQ","IWM","DIA",
]

print(f"Fetching {DAYS} days of 5-min bars for {len(symbols)} symbols...")
req = StockBarsRequest(
    symbol_or_symbols=symbols,
    timeframe=TimeFrame(5, TimeFrameUnit.Minute),
    start=datetime.now() - timedelta(days=DAYS),
)
bars = data_client.get_stock_bars(req)
df = bars.df

all_times = set()
data = {}

for sym in symbols:
    if sym not in df.index.get_level_values("symbol"):
        continue
    sym_df = df.xs(sym, level="symbol").copy()
    if len(sym_df) <= WARMUP + 10:
        continue
    close = sym_df["close"]
    high = sym_df["high"]
    low = sym_df["low"]
    volume = sym_df["volume"]
    n = len(sym_df)
    all_times.update(sym_df.index.tolist())

    # --- Vectorized rolling computations (single pass per indicator) ---
    # RSI(14)
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi_vals = 100 - (100 / (1 + rs))

    # MACD(8/21/5)
    ema_fast = close.ewm(span=8, adjust=False).mean()
    ema_slow = close.ewm(span=21, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    macd_signal = macd_line.ewm(span=5, adjust=False).mean()
    macd_hist = macd_line - macd_signal

    # SMAs
    sma_10 = close.rolling(10).mean()
    sma_20 = close.rolling(20).mean()

    # Bollinger %b position
    bb_sma = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bb_upper = bb_sma + 2 * bb_std
    bb_lower = bb_sma - 2 * bb_std
    bb_pos = (close - bb_lower) / (bb_upper - bb_lower)

    # Volume analysis (20-bar avg)
    vol_avg = volume.rolling(20).mean()
    vol_ratio = volume / vol_avg

    # Momentum(5)
    mom5 = close.pct_change(periods=5) * 100

    # Minute-level price change (last bar)
    price_chg = close.pct_change() * 100

    # --- Config values (constant per run) ---
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
    buy_min = Config.TA_MIN_BUY_SCORE  # default for pre-compute; we override at sweep
    sell_min = Config.TA_MIN_SELL_SCORE

    # --- Build per-bar score arrays (vectorized) ---
    buy_scores = np.zeros(n)
    sell_scores = np.zeros(n)
    prices = np.zeros(n)

    for i in range(n):
        prices[i] = float(round(close.iloc[i], 2))
        if i < WARMUP:
            continue

        # RSI sell grade
        r = rsi_vals.iloc[i]
        if pd.notna(r) and r > rsi_ob:
            rsi_sell = min(1.0, (r - rsi_ob) / (100 - rsi_ob)) * rsi_w
        else:
            rsi_sell = 0.0

        # MACD
        mh = macd_hist.iloc[i]
        mt = "bullish" if macd_line.iloc[i] > macd_signal.iloc[i] else "bearish"
        macd_buy = 1.0 if (pd.notna(mh) and mh > 0 and mt == "bullish") else 0.0
        macd_sell = 1.0 if (pd.notna(mh) and mh < 0 and mt == "bearish") else 0.0

        # Bollinger
        bp = bb_pos.iloc[i]
        if pd.notna(bp) and bp > bb_upper_thresh:
            bb_sell = min(1.0, (bp - bb_upper_thresh) / (1 - bb_upper_thresh)) * bb_w
        else:
            bb_sell = 0.0

        # Trend
        s10 = sma_10.iloc[i]
        s20 = sma_20.iloc[i]
        trend_buy = 1.0 if (pd.notna(s10) and pd.notna(s20) and s10 > s20) else 0.0
        trend_sell = 1.0 if (pd.notna(s10) and pd.notna(s20) and s10 < s20) else 0.0

        # Momentum
        m5 = mom5.iloc[i]
        m5 = m5 if pd.notna(m5) else 0.0
        if m5 > mom_thresh:
            mom_b = min(1.0, (m5 - mom_thresh) / mom_thresh) * mom_w
        else:
            mom_b = 0.0
        if m5 < -mom_thresh:
            mom_s = min(1.0, (abs(m5) - mom_thresh) / mom_thresh) * mom_w
        else:
            mom_s = 0.0

        # Volume boost
        vr = vol_ratio.iloc[i]
        vr = vr if pd.notna(vr) else 1.0
        boost = vol_boost_val if vr > vol_thresh else 1.0

        # Volume disqual
        curr_v = volume.iloc[i]

        bs = (macd_buy * macd_w + trend_buy * trend_w + mom_b) * boost
        ss = (rsi_sell + macd_sell * macd_w + bb_sell + trend_sell * trend_w + mom_s) * boost

        # Hard disqualifiers
        if pd.notna(r) and r > 80:
            bs = 0.0
        if curr_v < 1000:
            bs = 0.0

        buy_scores[i] = float(round(bs, 2))
        sell_scores[i] = float(round(ss, 2))

    data[sym] = {
        "prices": prices,
        "buy_scores": buy_scores,
        "sell_scores": sell_scores,
        "index": sym_df.index,
    }

all_times = sorted(all_times)
print(f"Loaded {len(data)} symbols, {len(all_times)} bars")


def run(min_buy_score):
    position = None
    trades = []
    cap = INITIAL_CAPITAL
    sell_min_local = Config.TA_MIN_SELL_SCORE

    for ts in all_times:
        if position is not None:
            sym = position["sym"]
            entry = data.get(sym)
            if entry is None:
                continue
            idx_lookup = entry["index"].get_indexer([ts])
            if idx_lookup[0] < 0:
                continue
            idx = idx_lookup[0]
            price = entry["prices"][idx]
            ss = entry["sell_scores"][idx]
            stop = position["entry_price"] * (1 + Config.TA_STOP_LOSS_PCT)
            if ss >= sell_min_local or price <= stop:
                exit_p = stop if price <= stop else price
                pnl = (exit_p - position["entry_price"]) / position["entry_price"]
                cap *= 1 + pnl
                trades.append({
                    "sym": sym, "pnl_pct": round(pnl * 100, 2),
                    "win": pnl > 0, "buy_score": position["buy_score"],
                    "sell_score": ss, "hit_stop": price <= stop,
                })
                position = None

        if position is None:
            candidates = []
            for sym, entry in data.items():
                idx_lookup = entry["index"].get_indexer([ts])
                if idx_lookup[0] < 0:
                    continue
                idx = idx_lookup[0]
                if idx < WARMUP:
                    continue
                bs = entry["buy_scores"][idx]
                if bs < min_buy_score:
                    continue
                candidates.append((sym, entry["prices"][idx], bs))
            if candidates:
                best = max(candidates, key=lambda x: x[2])
                position = {
                    "sym": best[0], "entry_price": best[1],
                    "buy_score": best[2],
                }

    return cap, trades


thresholds = [0.5, 1.0, 1.5, 2.0, 3.0]
results = []
for t in thresholds:
    print(f"Sweeping threshold {t} ...", flush=True)
    final_cap, trades = run(t)
    total_return = ((final_cap / INITIAL_CAPITAL) - 1) * 100
    wins = [x for x in trades if x["win"]]
    losses = [x for x in trades if not x["win"]]
    avg_win = np.mean([x["pnl_pct"] for x in wins]) if wins else 0
    avg_loss = np.mean([x["pnl_pct"] for x in losses]) if losses else 0
    max_dd = 0
    peak = INITIAL_CAPITAL
    running = INITIAL_CAPITAL
    for x in trades:
        running *= 1 + x["pnl_pct"] / 100
        if running > peak:
            peak = running
        dd = (peak - running) / peak * 100
        if dd > max_dd:
            max_dd = dd
    sharpe = 0
    if trades:
        rets = [x["pnl_pct"] for x in trades]
        if np.std(rets) > 0:
            sharpe = np.mean(rets) / np.std(rets)
    results.append({
        "threshold": t, "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "gross_return": total_return, "max_dd": max_dd, "sharpe": sharpe,
    })

print(f"\n{'='*100}")
print(f"{'5-Min Bar Backtest — Threshold Sweep':^100}")
print(f"{'='*100}")
print(f"{'Thresh':<8} {'Trades':<8} {'Win%':<8} {'Avg Win':<10} {'Avg Loss':<10} {'Return':<10} {'Max DD':<10} {'Sharpe':<8}")
print("-" * 100)
for r in results:
    print(f"{r['threshold']:<8} {r['trades']:<8} {r['win_rate']:<8.1f} {r['avg_win']:<+10.2f} {r['avg_loss']:<+10.2f} {r['gross_return']:<+10.2f} {r['max_dd']:<10.2f} {r['sharpe']:<8.2f}")
print("=" * 100)
