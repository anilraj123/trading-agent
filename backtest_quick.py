import os, sys, warnings, time
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

symbols = ['AAPL','MSFT','GOOGL','AMZN','NVDA','META','TSLA','JPM','V','JNJ',
           'WMT','PG','MA','UNH','HD','DIS','BAC','XOM','SPY','QQQ']

print("Fetching 180 days of data...")
start = datetime.now() - timedelta(days=180)
req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day, start=start)
bars = data_client.get_stock_bars(req)

data = {}
for sym in symbols:
    if sym in bars.df.index.get_level_values('symbol'):
        df = bars.df.xs(sym, level='symbol')
        if len(df) > 60:
            data[sym] = df
            print(f"  {sym}: {len(df)} bars")

def compute_signals(df):
    close = df['close']; high = df['high']; low = df['low']; volume = df['volume']
    sig = pd.DataFrame(index=df.index)
    sig['close'] = close

    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    sig['rsi'] = 100 - (100 / (1 + rs))

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    sig['macd'] = ema12 - ema26
    sig['macd_sig'] = sig['macd'].ewm(span=9, adjust=False).mean()

    sig['sma20'] = close.rolling(20).mean()
    sig['sma50'] = close.rolling(50).mean()

    bb_sma = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    sig['bb_upper'] = bb_sma + (bb_std * 2)
    sig['bb_lower'] = bb_sma - (bb_std * 2)
    sig['bb_pos'] = (close - sig['bb_lower']) / (sig['bb_upper'] - sig['bb_lower'])

    sig['momentum'] = (close / close.shift(10) - 1) * 100
    avg_vol = volume.rolling(20).mean()
    sig['vol_ratio'] = volume / avg_vol.replace(0, 1)
    return sig

print("\nComputing indicators...")
sig_cache = {sym: compute_signals(df) for sym, df in data.items()}

def run_backtest(params, sig_cache, capital=200.0):
    position = None
    trades = []
    all_dates = sorted(set().union(*[df.index for df in sig_cache.values()]))

    for date in all_dates:
        best_score = 0
        best_sym = None
        action = None

        for sym in sig_cache:
            sig = sig_cache[sym]
            if date not in sig.index:
                continue
            row = sig.loc[date]
            if pd.isna(row['close']) or pd.isna(row.get('rsi', 0)):
                continue

            buy = 0.0
            sell = 0.0

            if row['rsi'] < params['rsi_os']: buy += 1.0
            if row['rsi'] > params['rsi_ob']: sell += 1.0

            if len(sig) > 1:
                idx = sig.index.get_loc(date)
                if idx > 0:
                    prev = sig.iloc[idx-1]
                    if row['macd'] > row['macd_sig'] and prev['macd'] <= prev['macd_sig']:
                        buy += 1.0
                    if row['macd'] < row['macd_sig'] and prev['macd'] >= prev['macd_sig']:
                        sell += 1.0

            if not pd.isna(row['bb_pos']):
                if row['bb_pos'] < 0.05: buy += 1.0
                if row['bb_pos'] > 0.95: sell += 1.0

            if not pd.isna(row['sma20']) and not pd.isna(row['sma50']):
                if row['close'] > row['sma20'] and row['sma20'] > row['sma50']: buy += 0.5
                if row['close'] < row['sma20'] and row['sma20'] < row['sma50']: sell += 0.5

            if not pd.isna(row['momentum']):
                if row['momentum'] > params['mom_thresh']: buy += 0.5
                if row['momentum'] < -params['mom_thresh']: sell += 0.5

            if buy > 0 and row.get('vol_ratio', 0) > 1.5: buy *= params['vol_mult']
            if sell > 0 and row.get('vol_ratio', 0) > 1.5: sell *= params['vol_mult']

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
            capital *= (1 + pnl)
            trades.append({'sym': position['sym'], 'pnl': pnl * 100, 'win': pnl > 0})
            position = None

        if action == 'BUY' and position is None:
            price = sig_cache[best_sym].loc[date, 'close']
            position = {'sym': best_sym, 'entry': price, 'date': date}

    if not trades:
        return {'return': 0, 'win_rate': 0, 'trades': 0, 'capital': capital}

    wins = [t for t in trades if t['win']]
    ret = ((capital / 200) - 1) * 100
    return {
        'return': round(ret, 2),
        'win_rate': round(len(wins)/len(trades)*100, 1),
        'trades': len(trades),
        'capital': round(capital, 2),
        'avg_win': round(np.mean([t['pnl'] for t in wins]), 2) if wins else 0,
        'avg_loss': round(np.mean([t['pnl'] for t in trades if not t['win']]), 2) if len(trades) > len(wins) else 0
    }

print("\nTesting strategies...")
strategies = [
    {"name": "RSI Oversold Bounce", "params": {"rsi_os": 30, "rsi_ob": 70, "mom_thresh": 2.0, "vol_mult": 1.2, "min_buy": 1.5, "min_sell": 1.0}},
    {"name": "MACD Crossover", "params": {"rsi_os": 35, "rsi_ob": 65, "mom_thresh": 3.0, "vol_mult": 1.5, "min_buy": 1.0, "min_sell": 1.0}},
    {"name": "Trend Following", "params": {"rsi_os": 25, "rsi_ob": 75, "mom_thresh": 2.0, "vol_mult": 1.2, "min_buy": 2.0, "min_sell": 1.5}},
    {"name": "Mean Reversion", "params": {"rsi_os": 30, "rsi_ob": 70, "mom_thresh": 2.0, "vol_mult": 1.5, "min_buy": 1.5, "min_sell": 1.0}},
    {"name": "Conservative", "params": {"rsi_os": 25, "rsi_ob": 75, "mom_thresh": 3.0, "vol_mult": 1.2, "min_buy": 2.5, "min_sell": 1.5}},
    {"name": "Aggressive", "params": {"rsi_os": 35, "rsi_ob": 65, "mom_thresh": 1.0, "vol_mult": 1.5, "min_buy": 1.0, "min_sell": 0.5}},
]

results = []
for strat in strategies:
    t0 = time.time()
    res = run_backtest(strat["params"], sig_cache)
    res['name'] = strat["name"]
    res['time'] = round(time.time() - t0, 2)
    results.append(res)
    print(f"  {res['name']}: Return={res['return']}% | Win={res['win_rate']}% | Trades={res['trades']} | Capital=${res['capital']} | {res['time']}s")

print(f"\n{'='*80}")
print(f"BACKTEST RESULTS (20 stocks, 180 days, $200 starting)")
print(f"{'='*80}")
print(f"{'Strategy':<25} {'Return':>10} {'Win Rate':>10} {'Trades':>8} {'Capital':>10} {'Avg Win':>10} {'Avg Loss':>10}")
print(f"{'-'*80}")
for r in results:
    print(f"{r['name']:<25} {r['return']:>9}% {r['win_rate']:>9}% {r['trades']:>8} ${r['capital']:>9} {r.get('avg_win',0):>8}% {r.get('avg_loss',0):>8}%")

best = max(results, key=lambda x: x['return'])
print(f"\n{'='*80}")
print(f"BEST STRATEGY: {best['name']}")
print(f"  Return: {best['return']}% | Win Rate: {best['win_rate']}% | Trades: {best['trades']}")
print(f"  Final Capital: ${best['capital']}")
print(f"{'='*80}")
