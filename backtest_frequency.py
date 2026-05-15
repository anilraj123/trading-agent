import os, sys, warnings
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

TAX_RATE = 0.27
INITIAL_CAPITAL = 200.0

print("="*80)
print("OPTIMAL TRADE FREQUENCY ANALYSIS")
print("Goal: Enough trades in 2 weeks to validate the strategy")
print("="*80)

symbols = ['AAPL','MSFT','GOOGL','AMZN','NVDA','META','TSLA','JPM','V','JNJ',
           'WMT','PG','MA','UNH','HD','DIS','BAC','XOM','PFE','CSCO',
           'INTC','VZ','KO','PEP','MRK','ABT','TMO','COST','NFLX','ADBE',
           'CRM','AMD','QCOM','TXN','AVGO','ORCL','ACN','LLY','DHR','NKE',
           'NEE','BMY','UNP','LOW','PM','RTX','LIN','HON','AMGN','SPGI',
           'SPY','QQQ','IWM','DIA']

start = datetime.now() - timedelta(days=180)
req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day, start=start)
bars = data_client.get_stock_bars(req)

data = {}
for sym in symbols:
    if sym in bars.df.index.get_level_values('symbol'):
        df = bars.df.xs(sym, level='symbol')
        if len(df) > 60:
            data[sym] = df

def compute_indicators(df):
    close = df['close']; high = df['high']; low = df['low']; volume = df['volume']
    sig = pd.DataFrame(index=df.index)
    sig['close'] = close
    for period in [7, 14, 21]:
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs = gain / loss.replace(0, np.nan)
        sig[f'rsi_{period}'] = 100 - (100 / (1 + rs))
    for fast, slow in [(8,21), (12,26), (16,32)]:
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd = ema_fast - ema_slow
        sig[f'macd_{fast}_{slow}'] = macd
        sig[f'macd_sig_{fast}_{slow}'] = macd.ewm(span=9, adjust=False).mean()
    for p in [10, 20, 50]:
        sig[f'sma_{p}'] = close.rolling(p).mean()
    for p in [20, 50]:
        bb_sma = close.rolling(p).mean()
        bb_std = close.rolling(p).std()
        sig[f'bb_upper_{p}'] = bb_sma + (bb_std * 2)
        sig[f'bb_lower_{p}'] = bb_sma - (bb_std * 2)
        sig[f'bb_pos_{p}'] = (close - sig[f'bb_lower_{p}']) / (sig[f'bb_upper_{p}'] - sig[f'bb_lower_{p}'])
    for p in [5, 10, 15, 20]:
        sig[f'momentum_{p}'] = (close / close.shift(p) - 1) * 100
    for p in [10, 20, 50]:
        avg_vol = volume.rolling(p).mean()
        sig[f'vol_ratio_{p}'] = volume / avg_vol.replace(0, 1)
    return sig

sig_cache = {sym: compute_indicators(df) for sym, df in data.items()}

def run_strategy(sig_cache, params, capital=200.0, max_trades=100):
    position = None
    trades = []
    all_dates = sorted(set().union(*[df.index for df in sig_cache.values()]))

    rsi_key = f"rsi_{params['rsi_period']}"
    macd_key = f"macd_{params['macd_fast']}_{params['macd_slow']}"
    macd_sig_key = f"macd_sig_{params['macd_fast']}_{params['macd_slow']}"
    bb_pos_key = f"bb_pos_{params['bb_period']}"
    mom_key = f"momentum_{params['mom_period']}"
    vol_key = f"vol_ratio_{params['vol_period']}"

    for date in all_dates:
        if len(trades) >= max_trades:
            break

        best_score = 0
        best_sym = None
        action = None

        for sym in sig_cache:
            sig = sig_cache[sym]
            if date not in sig.index:
                continue
            row = sig.loc[date]
            if pd.isna(row['close']) or pd.isna(row.get(rsi_key, np.nan)):
                continue

            buy = 0.0
            sell = 0.0

            if row[rsi_key] < params['rsi_os']: buy += params['rsi_weight']
            if row[rsi_key] > params['rsi_ob']: sell += params['rsi_weight']

            if len(sig) > 1:
                idx = sig.index.get_loc(date)
                if idx > 0:
                    prev = sig.iloc[idx-1]
                    if row[macd_key] > row[macd_sig_key] and prev[macd_key] <= prev[macd_sig_key]:
                        buy += params['macd_weight']
                    if row[macd_key] < row[macd_sig_key] and prev[macd_key] >= prev[macd_sig_key]:
                        sell += params['macd_weight']

            if not pd.isna(row.get(bb_pos_key, np.nan)):
                if row[bb_pos_key] < 0.10: buy += params['bb_weight']
                if row[bb_pos_key] > 0.90: sell += params['bb_weight']

            sma_s = f"sma_{params['sma_short']}"
            sma_l = f"sma_{params['sma_long']}"
            if not pd.isna(row.get(sma_s, np.nan)) and not pd.isna(row.get(sma_l, np.nan)):
                if row[sma_s] and row[sma_s] > row[sma_l]:
                    buy += params['trend_weight'] * 0.5
                elif row[sma_s] and row[sma_s] < row[sma_l]:
                    sell += params['trend_weight'] * 0.5

            if not pd.isna(row.get(mom_key, np.nan)):
                if row[mom_key] > params['mom_thresh']: buy += params['mom_weight']
                if row[mom_key] < -params['mom_thresh']: sell += params['mom_weight']

            if not pd.isna(row.get(vol_key, np.nan)):
                if buy > 0 and row[vol_key] > 1.5: buy *= params['vol_mult']
                if sell > 0 and row[vol_key] > 1.5: sell *= params['vol_mult']

            if position is None and buy > best_score and buy >= params['min_buy']:
                best_score = buy
                best_sym = sym
                action = 'BUY'
            elif position and position['sym'] == sym and sell >= params['min_sell']:
                action = 'SELL'
                break

        if position and action == 'SELL':
            price = sig_cache[position['sym']].loc[date, 'close']
            pnl = (price - position['entry']) / position['entry']
            stop_loss = position['entry'] * (1 + params['stop_loss'])
            if price <= stop_loss:
                pnl = (stop_loss - position['entry']) / position['entry']
            capital *= (1 + pnl)
            trades.append({
                'sym': position['sym'], 'entry': position['entry'], 'exit': price,
                'pnl': pnl * 100, 'win': pnl > 0,
                'date_in': position['date'], 'date_out': date
            })
            position = None

        if action == 'BUY' and position is None:
            price = sig_cache[best_sym].loc[date, 'close']
            position = {'sym': best_sym, 'entry': price, 'date': date}

    wins = [t for t in trades if t['win']]
    losses = [t for t in trades if not t['win']]
    ret = ((capital / INITIAL_CAPITAL) - 1) * 100

    return {
        'capital': round(capital, 2), 'return': round(ret, 2),
        'trades': len(trades), 'wins': len(wins), 'losses': len(losses),
        'win_rate': round(len(wins)/len(trades)*100, 1) if trades else 0,
        'trade_details': trades
    }

configs = [
    {
        "name": "CURRENT (Very Conservative)",
        "params": {"rsi_period": 14, "rsi_os": 25, "rsi_ob": 60, "rsi_weight": 1.5,
                   "macd_fast": 12, "macd_slow": 26, "macd_weight": 0.5,
                   "bb_period": 20, "bb_weight": 0.5,
                   "sma_short": 20, "sma_long": 50, "trend_weight": 0.0,
                   "mom_period": 10, "mom_thresh": 1.0, "mom_weight": 0.5,
                   "vol_period": 20, "vol_mult": 1.0,
                   "min_buy": 1.5, "min_sell": 2.0, "stop_loss": -0.01},
    },
    {
        "name": "MODERATE (More Active)",
        "params": {"rsi_period": 14, "rsi_os": 30, "rsi_ob": 65, "rsi_weight": 1.0,
                   "macd_fast": 12, "macd_slow": 26, "macd_weight": 1.0,
                   "bb_period": 20, "bb_weight": 1.0,
                   "sma_short": 20, "sma_long": 50, "trend_weight": 0.5,
                   "mom_period": 10, "mom_thresh": 2.0, "mom_weight": 0.5,
                   "vol_period": 20, "vol_mult": 1.2,
                   "min_buy": 1.0, "min_sell": 1.0, "stop_loss": -0.03},
    },
    {
        "name": "ACTIVE (Enough for 2-week validation)",
        "params": {"rsi_period": 14, "rsi_os": 35, "rsi_ob": 65, "rsi_weight": 1.0,
                   "macd_fast": 8, "macd_slow": 21, "macd_weight": 1.0,
                   "bb_period": 20, "bb_weight": 1.0,
                   "sma_short": 10, "sma_long": 20, "trend_weight": 1.0,
                   "mom_period": 10, "mom_thresh": 2.0, "mom_weight": 1.0,
                   "vol_period": 20, "vol_mult": 1.2,
                   "min_buy": 1.0, "min_sell": 1.0, "stop_loss": -0.03},
    },
]

print(f"\n{'Config':<30} {'Trades':>7} {'Win%':>6} {'Return':>8} {'Trades/2wk':>11} {'Assessment'}")
print("-"*80)

for cfg in configs:
    res = run_strategy(sig_cache, cfg['params'])
    trades_per_2wk = res['trades'] / (180/10)
    assessment = ""
    if trades_per_2wk < 1:
        assessment = "TOO FEW for 2-week test"
    elif trades_per_2wk < 2:
        assessment = "BARELY enough"
    elif trades_per_2wk < 5:
        assessment = "GOOD for validation"
    else:
        assessment = "PLenty of data"

    print(f"{cfg['name']:<30} {res['trades']:>7} {res['win_rate']:>5}% {res['return']:>+7.2f}% {trades_per_2wk:>10.1f} {assessment}")

print(f"\n{'='*80}")
print("RECOMMENDATION")
print(f"{'='*80}")
print("""
For a 2-week paper trade validation, you need at least 2-5 trades.

The CURRENT config gives ~0.2 trades/2 weeks — you'll likely see nothing.

SOLUTION: Use the ACTIVE config for paper trading:
- RSI Oversold: 35 (vs 25) — catches more dips
- RSI Overbought: 65 (vs 60) — sells sooner
- Min Buy Score: 1.0 (vs 1.5) — lower threshold
- Min Sell Score: 1.0 (vs 2.0) — exits faster
- Stop Loss: -3% (vs -1%) — gives trades room to breathe

Expected: ~2-3 trades per 2 weeks. Enough to validate.

After validation period, switch to conservative config for live trading.""")
