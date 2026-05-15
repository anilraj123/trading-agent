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
print("COMPREHENSIVE BACKTESTING ENGINE - OPTIMIZED")
print("="*80)

print(f"\nFetching 180 days of data for {len(symbols)} stocks...")
start = datetime.now() - timedelta(days=180)
t0 = time.time()
req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day, start=start)
bars = data_client.get_stock_bars(req)

data = {}
for sym in symbols:
    if sym in bars.df.index.get_level_values('symbol'):
        df = bars.df.xs(sym, level='symbol')
        if len(df) > 60:
            data[sym] = df

print(f"Fetched data for {len(data)} stocks in {time.time()-t0:.1f}s")

print("\nComputing technical indicators...")
t0 = time.time()

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

    for p in [14, 20]:
        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        sig[f'atr_{p}'] = tr.rolling(p).mean()

    for p in [10, 20, 50]:
        avg_vol = volume.rolling(p).mean()
        sig[f'vol_ratio_{p}'] = volume / avg_vol.replace(0, 1)

    sig['daily_return'] = close.pct_change()
    sig['high_low_pct'] = (high - low) / close

    return sig

sig_cache = {}
for sym, df in data.items():
    sig_cache[sym] = compute_all_indicators(df)

print(f"Computed indicators for {len(sig_cache)} stocks in {time.time()-t0:.1f}s")

def run_backtest_fast(params, sig_cache, capital=200.0):
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
            trades.append({'sym': position['sym'], 'entry': position['entry'], 'exit': price,
                          'pnl': pnl * 100, 'win': pnl > 0, 'date': date})
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
        'worst_trade': round(min([t['pnl'] for t in trades]), 2),
        'consec_wins': max_consecutive(wins), 'consec_losses': max_consecutive(losses, invert=True)
    }

def max_consecutive(trades, invert=False):
    max_c = 0; curr = 0
    for t in trades:
        if (t['win'] and not invert) or (not t['win'] and invert):
            curr += 1; max_c = max(max_c, curr)
        else:
            curr = 0
    return max_c

print("\n" + "="*80)
print("PHASE 1: STRATEGY ARCHETYPE TESTING (6 strategies)")
print("="*80)

archetypes = [
    {"name": "Mean Reversion (RSI 30)",
     "params": {"rsi_period": 14, "rsi_os": 30, "rsi_ob": 70, "rsi_weight": 1.5,
                "macd_fast": 12, "macd_slow": 26, "macd_weight": 1.0,
                "bb_period": 20, "bb_weight": 1.5,
                "sma_short": 20, "sma_long": 50, "trend_weight": 0.5,
                "mom_period": 10, "mom_thresh": 2.0, "mom_weight": 0.5,
                "vol_period": 20, "vol_mult": 1.5,
                "min_buy": 1.5, "min_sell": 1.0, "stop_loss": -0.05}},
    {"name": "Mean Reversion (RSI 25)",
     "params": {"rsi_period": 14, "rsi_os": 25, "rsi_ob": 75, "rsi_weight": 2.0,
                "macd_fast": 12, "macd_slow": 26, "macd_weight": 1.0,
                "bb_period": 20, "bb_weight": 2.0,
                "sma_short": 20, "sma_long": 50, "trend_weight": 0.5,
                "mom_period": 10, "mom_thresh": 3.0, "mom_weight": 0.5,
                "vol_period": 20, "vol_mult": 1.5,
                "min_buy": 2.0, "min_sell": 1.5, "stop_loss": -0.03}},
    {"name": "MACD Crossover Focus",
     "params": {"rsi_period": 14, "rsi_os": 30, "rsi_ob": 70, "rsi_weight": 1.0,
                "macd_fast": 12, "macd_slow": 26, "macd_weight": 2.0,
                "bb_period": 20, "bb_weight": 1.0,
                "sma_short": 20, "sma_long": 50, "trend_weight": 1.0,
                "mom_period": 10, "mom_thresh": 2.0, "mom_weight": 1.0,
                "vol_period": 20, "vol_mult": 1.2,
                "min_buy": 1.5, "min_sell": 1.5, "stop_loss": -0.05}},
    {"name": "Trend Following",
     "params": {"rsi_period": 14, "rsi_os": 40, "rsi_ob": 60, "rsi_weight": 0.5,
                "macd_fast": 12, "macd_slow": 26, "macd_weight": 1.5,
                "bb_period": 20, "bb_weight": 0.5,
                "sma_short": 20, "sma_long": 50, "trend_weight": 2.0,
                "mom_period": 20, "mom_thresh": 5.0, "mom_weight": 2.0,
                "vol_period": 20, "vol_mult": 1.5,
                "min_buy": 2.5, "min_sell": 2.0, "stop_loss": -0.07}},
    {"name": "Momentum Breakout",
     "params": {"rsi_period": 14, "rsi_os": 30, "rsi_ob": 80, "rsi_weight": 0.5,
                "macd_fast": 8, "macd_slow": 21, "macd_weight": 1.5,
                "bb_period": 20, "bb_weight": 0.5,
                "sma_short": 10, "sma_long": 20, "trend_weight": 1.5,
                "mom_period": 5, "mom_thresh": 3.0, "mom_weight": 2.0,
                "vol_period": 10, "vol_mult": 2.0,
                "min_buy": 2.0, "min_sell": 1.5, "stop_loss": -0.03}},
    {"name": "Conservative Dip Buy",
     "params": {"rsi_period": 14, "rsi_os": 20, "rsi_ob": 80, "rsi_weight": 2.0,
                "macd_fast": 12, "macd_slow": 26, "macd_weight": 1.0,
                "bb_period": 50, "bb_weight": 2.0,
                "sma_short": 20, "sma_long": 50, "trend_weight": 1.0,
                "mom_period": 15, "mom_thresh": 5.0, "mom_weight": 0.5,
                "vol_period": 20, "vol_mult": 1.5,
                "min_buy": 3.0, "min_sell": 2.0, "stop_loss": -0.02}},
]

arch_results = []
for strat in archetypes:
    t0 = time.time()
    res = run_backtest_fast(strat["params"], sig_cache)
    if res:
        res['name'] = strat["name"]
        res['time'] = round(time.time() - t0, 3)
        arch_results.append(res)
        print(f"  {res['name']:30s} | Ret: {res['return']:>6}% | Win: {res['win_rate']:>5}% | Trades: {res['trades']:>3} | Sharpe: {res['sharpe']:>5} | MaxDD: {res['max_drawdown']:>6}% | ${res['capital']}")

best_arch = max(arch_results, key=lambda x: x['return'])
print(f"\nBest archetype: {best_arch['name']} ({best_arch['return']}% return)")

print("\n" + "="*80)
print("PHASE 2: GRID SEARCH (focused on best archetype parameters)")
print("="*80)

base = best_arch['params']

param_grid = {
    'rsi_os': [20, 25, 30, 35],
    'rsi_ob': [65, 70, 75, 80],
    'rsi_weight': [1.0, 1.5, 2.0],
    'macd_weight': [0.5, 1.0, 1.5],
    'bb_weight': [0.5, 1.0, 1.5],
    'trend_weight': [0.5, 1.0, 1.5],
    'mom_weight': [0.5, 1.0, 1.5],
    'mom_thresh': [2.0, 3.0, 5.0],
    'vol_mult': [1.2, 1.5, 2.0],
    'min_buy': [1.0, 1.5, 2.0, 2.5],
    'min_sell': [0.5, 1.0, 1.5],
    'stop_loss': [-0.02, -0.03, -0.05, -0.07],
    'rsi_period': [base['rsi_period']],
    'macd_fast': [base['macd_fast']],
    'macd_slow': [base['macd_slow']],
    'bb_period': [base['bb_period']],
    'sma_short': [base['sma_short']],
    'sma_long': [base['sma_long']],
    'mom_period': [base['mom_period']],
    'vol_period': [base['vol_period']],
}

param_names = list(param_grid.keys())
param_values = list(param_grid.values())
total_combos = np.prod([len(v) for v in param_values])
print(f"Testing {total_combos:,} combinations...\n")

best_params = None
best_score = -999
grid_results = []
count = 0
t_start = time.time()

from itertools import product

for combo in product(*param_values):
    count += 1
    params = dict(zip(param_names, combo))

    try:
        res = run_backtest_fast(params, sig_cache)
        if not res or res['trades'] < 3:
            continue

        score = (res['return'] * 0.25 +
                res['win_rate'] * 0.15 +
                res['sharpe'] * 0.25 +
                res['profit_factor'] * 0.15 +
                (abs(res['max_drawdown']) * -0.2))

        grid_results.append({**params, **res, 'score': round(score, 2)})

        if score > best_score:
            best_score = score
            best_params = params.copy()

        if count % 1000 == 0:
            elapsed = time.time() - t_start
            eta = (elapsed / count) * (total_combos - count)
            print(f"  [{count:>6}/{total_combos}] Score: {best_score:.2f} | Ret: {grid_results[-1]['return'] if grid_results else 'N/A'}% | ETA: {eta/60:.1f}m")

    except Exception as e:
        continue

print(f"\nGrid search complete. Tested {count} valid combinations in {time.time()-t_start:.0f}s")

if grid_results:
    grid_df = pd.DataFrame(grid_results)
    grid_df = grid_df.sort_values('score', ascending=False)
    grid_df.to_csv("backtest_full_results.csv", index=False)

    print(f"\n{'='*80}")
    print("TOP 10 PARAMETER SETS")
    print(f"{'='*80}")
    print(f"{'#':>3} {'Return':>8} {'Win%':>6} {'Trades':>7} {'Sharpe':>7} {'MaxDD':>7} {'PF':>5} {'RSI_OS':>6} {'RSI_OB':>6} {'MinBuy':>6} {'Stop':>6} {'Score':>7}")
    print(f"{'-'*100}")

    for i, (_, row) in enumerate(grid_df.head(10).iterrows()):
        print(f"{i+1:>3} {row['return']:>7}% {row['win_rate']:>5}% {row['trades']:>7} {row['sharpe']:>7} {row['max_drawdown']:>6}% {row['profit_factor']:>5} {row['rsi_os']:>6} {row['rsi_ob']:>6} {row['min_buy']:>6} {row['stop_loss']:>6} {row['score']:>7}")

    best = grid_df.iloc[0]
    print(f"\n{'='*80}")
    print(f"OPTIMAL STRATEGY")
    print(f"{'='*80}")
    print(f"Total Return: {best['return']:.2f}%")
    print(f"Win Rate: {best['win_rate']:.1f}%")
    print(f"Total Trades: {int(best['trades'])}")
    print(f"Sharpe Ratio: {best['sharpe']:.2f}")
    print(f"Max Drawdown: {best['max_drawdown']:.2f}%")
    print(f"Profit Factor: {best['profit_factor']:.2f}")
    print(f"Final Capital: ${best['capital']:.2f}")
    print(f"Avg Win: {best['avg_win']:.2f}% | Avg Loss: {best['avg_loss']:.2f}%")
    print(f"Best Trade: {best['best_trade']:.2f}% | Worst Trade: {best['worst_trade']:.2f}%")
    print(f"Consecutive Wins: {int(best['consec_wins'])} | Consecutive Losses: {int(best['consec_losses'])}")

    print(f"\nOptimal Parameters:")
    print(f"  RSI Oversold: {int(best['rsi_os'])}")
    print(f"  RSI Overbought: {int(best['rsi_ob'])}")
    print(f"  RSI Weight: {best['rsi_weight']:.1f}")
    print(f"  MACD Weight: {best['macd_weight']:.1f}")
    print(f"  Bollinger Weight: {best['bb_weight']:.1f}")
    print(f"  Trend Weight: {best['trend_weight']:.1f}")
    print(f"  Momentum Threshold: {best['mom_thresh']:.1f}")
    print(f"  Volume Multiplier: {best['vol_mult']:.1f}")
    print(f"  Min Buy Score: {best['min_buy']:.1f}")
    print(f"  Stop Loss: {best['stop_loss']:.0%}")
