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
           'WMT','PG','MA','UNH','HD','DIS','BAC','XOM','PFE','CSCO',
           'INTC','VZ','KO','PEP','MRK','ABT','TMO','COST','NFLX','ADBE',
           'CRM','AMD','QCOM','TXN','AVGO','ORCL','ACN','LLY','DHR','NKE',
           'NEE','BMY','UNP','LOW','PM','RTX','LIN','HON','AMGN','SPGI',
           'SPY','QQQ','IWM','DIA']

print("="*80)
print("SEQUENTIAL PARAMETER OPTIMIZATION BACKTEST")
print("="*80)

print(f"\nFetching 180 days of data for {len(symbols)} stocks...")
start = datetime.now() - timedelta(days=180)
req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day, start=start)
bars = data_client.get_stock_bars(req)

data = {}
for sym in symbols:
    if sym in bars.df.index.get_level_values('symbol'):
        df = bars.df.xs(sym, level='symbol')
        if len(df) > 60:
            data[sym] = df

print(f"Fetched data for {len(data)} stocks")

def compute_all_indicators(df):
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

sig_cache = {}
for sym, df in data.items():
    sig_cache[sym] = compute_all_indicators(df)

print(f"Computed indicators for {len(sig_cache)} stocks")

def run_backtest(params, sig_cache, capital=200.0):
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
                if row[bb_pos_key] < 0.05: buy += params['bb_weight']
                if row[bb_pos_key] > 0.95: sell += params['bb_weight']

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
            trades.append({'sym': position['sym'], 'pnl': pnl * 100, 'win': pnl > 0, 'date': date})
            position = None

        if action == 'BUY' and position is None:
            price = sig_cache[best_sym].loc[date, 'close']
            position = {'sym': best_sym, 'entry': price, 'date': date}

    if not trades:
        return None

    wins = [t for t in trades if t['win']]
    losses = [t for t in trades if not t['win']]
    ret = ((capital / 200) - 1) * 100

    equity_series = pd.Series([t['pnl'] for t in trades]).cumsum() + 200
    running_max = equity_series.expanding().max()
    drawdowns = (equity_series - running_max) / running_max * 100
    max_dd = drawdowns.min()

    daily_returns = pd.Series([t['pnl'] for t in trades])
    sharpe = (daily_returns.mean() / daily_returns.std() * np.sqrt(len(trades))) if daily_returns.std() > 0 else 0

    gross_profit = sum([t['pnl'] for t in wins]) if wins else 0
    gross_loss = abs(sum([t['pnl'] for t in losses])) if losses else 1
    profit_factor = gross_profit / gross_loss

    return {
        'return': round(ret, 2), 'win_rate': round(len(wins)/len(trades)*100, 1),
        'trades': len(trades), 'capital': round(capital, 2),
        'avg_win': round(np.mean([t['pnl'] for t in wins]), 2) if wins else 0,
        'avg_loss': round(np.mean([t['pnl'] for t in losses]), 2) if losses else 0,
        'max_drawdown': round(max_dd, 2), 'sharpe': round(sharpe, 2),
        'profit_factor': round(profit_factor, 2),
        'best_trade': round(max([t['pnl'] for t in trades]), 2),
        'worst_trade': round(min([t['pnl'] for t in trades]), 2)
    }

base = {
    'rsi_period': 14, 'rsi_os': 30, 'rsi_ob': 70, 'rsi_weight': 1.5,
    'macd_fast': 12, 'macd_slow': 26, 'macd_weight': 1.0,
    'bb_period': 20, 'bb_weight': 1.5,
    'sma_short': 20, 'sma_long': 50, 'trend_weight': 0.5,
    'mom_period': 10, 'mom_thresh': 2.0, 'mom_weight': 0.5,
    'vol_period': 20, 'vol_mult': 1.5,
    'min_buy': 1.5, 'min_sell': 1.0, 'stop_loss': -0.05
}

print("\n" + "="*80)
print("SEQUENTIAL OPTIMIZATION (one parameter at a time)")
print("="*80)

results_summary = []
current = base.copy()

optimization_order = [
    ('rsi_os', [20, 25, 30, 35, 40]),
    ('rsi_ob', [60, 65, 70, 75, 80]),
    ('rsi_weight', [0.5, 1.0, 1.5, 2.0, 2.5]),
    ('macd_weight', [0.5, 1.0, 1.5, 2.0, 2.5]),
    ('bb_weight', [0.5, 1.0, 1.5, 2.0, 2.5]),
    ('trend_weight', [0.0, 0.5, 1.0, 1.5, 2.0]),
    ('mom_weight', [0.0, 0.5, 1.0, 1.5, 2.0]),
    ('mom_thresh', [1.0, 2.0, 3.0, 5.0, 8.0]),
    ('vol_mult', [1.0, 1.2, 1.5, 2.0, 3.0]),
    ('min_buy', [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]),
    ('min_sell', [0.5, 1.0, 1.5, 2.0]),
    ('stop_loss', [-0.01, -0.02, -0.03, -0.05, -0.07, -0.10]),
]

t_start = time.time()
for param_name, values in optimization_order:
    print(f"\nOptimizing {param_name}: {values}")
    best_val = current[param_name]
    best_score = -999

    for val in values:
        test = current.copy()
        test[param_name] = val
        res = run_backtest(test, sig_cache)
        if res and res['trades'] >= 3:
            score = res['return'] * 0.3 + res['win_rate'] * 0.2 + res['sharpe'] * 0.3 + res['profit_factor'] * 0.2
            print(f"  {param_name}={val:>8}: Ret={res['return']:>6}% | Win={res['win_rate']:>5}% | Trades={res['trades']:>3} | Sharpe={res['sharpe']:>5} | MaxDD={res['max_drawdown']:>6}% | Score={score:.2f}")
            if score > best_score:
                best_score = score
                best_val = val
        else:
            print(f"  {param_name}={val:>8}: NO TRADES")

    current[param_name] = best_val
    print(f"  -> Selected: {param_name}={best_val}")
    results_summary.append({'param': param_name, 'best_value': best_val, 'score': best_score})

print(f"\nSequential optimization complete in {time.time()-t_start:.0f}s")

final_res = run_backtest(current, sig_cache)

print(f"\n{'='*80}")
print("OPTIMAL PARAMETER SET (Sequential Optimization)")
print(f"{'='*80}")
print(f"\nReturn: {final_res['return']:.2f}%")
print(f"Win Rate: {final_res['win_rate']:.1f}%")
print(f"Total Trades: {final_res['trades']}")
print(f"Sharpe Ratio: {final_res['sharpe']:.2f}")
print(f"Max Drawdown: {final_res['max_drawdown']:.2f}%")
print(f"Profit Factor: {final_res['profit_factor']:.2f}")
print(f"Final Capital: ${final_res['capital']:.2f}")
print(f"Avg Win: {final_res['avg_win']:.2f}% | Avg Loss: {final_res['avg_loss']:.2f}%")
print(f"Best Trade: {final_res['best_trade']:.2f}% | Worst Trade: {final_res['worst_trade']:.2f}%")

print(f"\nOptimal Parameters:")
for k, v in current.items():
    print(f"  {k}: {v}")

print(f"\nApplying to live bot configuration...")

import json

params_for_bot = {
    'RSI_OVERSOLD': current['rsi_os'],
    'RSI_OVERBOUGHT': current['rsi_ob'],
    'RSI_WEIGHT': current['rsi_weight'],
    'MACD_WEIGHT': current['macd_weight'],
    'BB_WEIGHT': current['bb_weight'],
    'TREND_WEIGHT': current['trend_weight'],
    'MOM_WEIGHT': current['mom_weight'],
    'MOM_THRESHOLD': current['mom_thresh'],
    'VOLUME_MULTIPLIER': current['vol_mult'],
    'MIN_BUY_SCORE': current['min_buy'],
    'MIN_SELL_SCORE': current['min_sell'],
    'STOP_LOSS_PCT': current['stop_loss']
}

with open("optimal_params.json", "w") as f:
    json.dump(params_for_bot, f, indent=2)

print(f"Saved optimal parameters to optimal_params.json")
