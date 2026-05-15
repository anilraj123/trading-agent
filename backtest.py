import os
import sys
import numpy as np
import pandas as pd
import warnings
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

def fetch_historical_data(symbols: list, days: int = 180) -> dict:
    print(f"Fetching {days} days of data for {len(symbols)} stocks...")
    start = datetime.now() - timedelta(days=days)

    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start
    )
    bars = data_client.get_stock_bars(req)

    all_data = {}
    for symbol in symbols:
        try:
            if symbol in bars.df.index.get_level_values('symbol'):
                df = bars.df.xs(symbol, level='symbol')
                if len(df) > 60:
                    all_data[symbol] = df
                    print(f"  {symbol}: {len(df)} bars")
        except Exception as e:
            print(f"  {symbol}: Failed - {e}")
    return all_data

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df['close']
    signals = pd.DataFrame(index=df.index)
    signals['close'] = close

    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss.replace(0, np.nan)
    signals['rsi'] = 100 - (100 / (1 + rs))

    ema_fast = close.ewm(span=12, adjust=False).mean()
    ema_slow = close.ewm(span=26, adjust=False).mean()
    signals['macd'] = ema_fast - ema_slow
    signals['macd_signal'] = signals['macd'].ewm(span=9, adjust=False).mean()

    signals['sma_20'] = close.rolling(window=20).mean()
    signals['sma_50'] = close.rolling(window=50).mean()

    bb_sma = close.rolling(window=20).mean()
    bb_std = close.rolling(window=20).std()
    signals['bb_upper'] = bb_sma + (bb_std * 2)
    signals['bb_lower'] = bb_sma - (bb_std * 2)
    signals['bb_position'] = (close - signals['bb_lower']) / (signals['bb_upper'] - signals['bb_lower'])

    signals['momentum'] = (close / close.shift(10) - 1) * 100

    volume = df['volume']
    avg_volume = volume.rolling(window=20).mean()
    signals['volume_ratio'] = volume / avg_volume.replace(0, 1)

    return signals

def generate_signals(signals: pd.DataFrame, params: dict) -> pd.DataFrame:
    df = signals.copy()
    df['buy_score'] = 0.0
    df['sell_score'] = 0.0

    df.loc[df['rsi'] < params['rsi_oversold'], 'buy_score'] += params['rsi_weight']
    df.loc[df['rsi'] > params['rsi_overbought'], 'sell_score'] += params['rsi_weight']

    macd_prev = df['macd'].shift(1)
    signal_prev = df['macd_signal'].shift(1)
    macd_cross_bullish = (df['macd'] > df['macd_signal']) & (macd_prev <= signal_prev)
    macd_cross_bearish = (df['macd'] < df['macd_signal']) & (macd_prev >= signal_prev)
    df.loc[macd_cross_bullish, 'buy_score'] += params['macd_weight']
    df.loc[macd_cross_bearish, 'sell_score'] += params['macd_weight']

    df.loc[df['bb_position'] < 0.05, 'buy_score'] += params['bb_weight']
    df.loc[df['bb_position'] > 0.95, 'sell_score'] += params['bb_weight']

    df.loc[(df['close'] > df['sma_20']) & (df['sma_20'] > df['sma_50']), 'buy_score'] += params['trend_weight'] * 0.5
    df.loc[(df['close'] < df['sma_20']) & (df['sma_20'] < df['sma_50']), 'sell_score'] += params['trend_weight'] * 0.5

    df.loc[df['momentum'] > params['momentum_threshold'], 'buy_score'] += params['momentum_weight']
    df.loc[df['momentum'] < -params['momentum_threshold'], 'sell_score'] += params['momentum_weight']

    df.loc[(df['buy_score'] > 0) & (df['volume_ratio'] > 1.5), 'buy_score'] *= params['volume_multiplier']
    df.loc[(df['sell_score'] > 0) & (df['volume_ratio'] > 1.5), 'sell_score'] *= params['volume_multiplier']

    return df

def backtest_strategy(signals_cache: dict, params: dict, initial_capital: float = 200.0) -> dict:
    position = None
    capital = initial_capital
    equity_curve = []
    trades = []

    symbols = list(signals_cache.keys())
    all_dates = sorted(set().union(*[df.index for df in signals_cache.values()]))

    for date in all_dates:
        best_score = 0
        best_symbol = None
        best_action = None

        for symbol in symbols:
            sig = signals_cache[symbol]
            if date not in sig.index:
                continue
            row = sig.loc[date]
            if pd.isna(row['close']) or pd.isna(row.get('buy_score', 0)):
                continue

            buy_score = row.get('buy_score', 0)
            sell_score = row.get('sell_score', 0)

            if position is None and buy_score > best_score and buy_score >= params['min_buy_score']:
                best_score = buy_score
                best_symbol = symbol
                best_action = 'BUY'

            elif position and position['symbol'] == symbol and sell_score >= params['min_sell_score']:
                best_action = 'SELL'
                best_symbol = symbol
                break

        if position and best_action == 'SELL':
            current_price = signals_cache[position['symbol']].loc[date, 'close']
            pnl = (current_price - position['entry_price']) / position['entry_price']
            capital *= (1 + pnl)
            trades.append({
                'entry_date': position['date'],
                'exit_date': date,
                'symbol': position['symbol'],
                'entry_price': position['entry_price'],
                'exit_price': current_price,
                'pnl_pct': pnl * 100,
                'result': 'WIN' if pnl > 0 else 'LOSS'
            })
            position = None

        if best_action == 'BUY' and position is None:
            entry_price = signals_cache[best_symbol].loc[date, 'close']
            position = {
                'symbol': best_symbol,
                'entry_price': entry_price,
                'date': date
            }

        current_equity = capital
        if position:
            try:
                current_price = signals_cache[position['symbol']].loc[date, 'close']
                position_value = capital * (1 + (current_price - position['entry_price']) / position['entry_price'])
                current_equity = position_value
            except:
                pass

        equity_curve.append({'date': date, 'equity': current_equity})

    if not trades:
        return {
            'total_return_pct': 0, 'win_rate': 0, 'total_trades': 0,
            'sharpe_ratio': 0, 'max_drawdown_pct': 0, 'profit_factor': 0,
            'avg_win_pct': 0, 'avg_loss_pct': 0, 'equity_curve': equity_curve, 'trades': []
        }

    wins = [t['pnl_pct'] for t in trades if t['result'] == 'WIN']
    losses = [t['pnl_pct'] for t in trades if t['result'] == 'LOSS']

    final_equity = equity_curve[-1]['equity'] if equity_curve else initial_capital
    total_return = ((final_equity / initial_capital) - 1) * 100

    equity_series = pd.Series([e['equity'] for e in equity_curve])
    running_max = equity_series.expanding().max()
    drawdowns = (equity_series - running_max) / running_max * 100
    max_drawdown = drawdowns.min()

    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 1
    profit_factor = gross_profit / gross_loss

    return {
        'total_return_pct': round(total_return, 2),
        'win_rate': round(len(wins) / len(trades) * 100, 2) if trades else 0,
        'total_trades': len(trades),
        'sharpe_ratio': 0,
        'max_drawdown_pct': round(max_drawdown, 2),
        'profit_factor': round(profit_factor, 2),
        'avg_win_pct': round(np.mean(wins), 2) if wins else 0,
        'avg_loss_pct': round(np.mean(losses), 2) if losses else 0,
        'equity_curve': equity_curve,
        'trades': trades,
        'final_equity': round(final_equity, 2)
    }

def optimize_parameters(data: dict) -> pd.DataFrame:
    print("\n" + "="*60)
    print("PARAMETER OPTIMIZATION")
    print("="*60)

    param_grid = {
        'rsi_oversold': [30, 35],
        'rsi_overbought': [65, 70],
        'rsi_weight': [1.0, 1.5],
        'macd_weight': [1.0, 1.5],
        'bb_weight': [1.0, 1.5],
        'trend_weight': [0.5, 1.0],
        'momentum_weight': [0.5, 1.0],
        'momentum_threshold': [2.0, 3.0],
        'volume_multiplier': [1.2, 1.5],
        'min_buy_score': [1.5, 2.0],
        'min_sell_score': [1.0, 1.5],
        'position_size_pct': [0.10, 0.15],
        'min_hold_days': [1, 3]
    }

    best_params = None
    best_score = -999
    results = []

    param_names = list(param_grid.keys())
    param_values = list(param_grid.values())

    from itertools import product
    total_combos = np.prod([len(v) for v in param_values])
    print(f"Testing {total_combos} combinations...\n")

    signals_cache = {}
    for symbol, df in data.items():
        signals_cache[symbol] = compute_indicators(df)
    print("Indicators pre-computed for all stocks.\n")

    count = 0
    for combo in product(*param_values):
        count += 1
        params = dict(zip(param_names, combo))

        signals_with_scores = {}
        for symbol, sig in signals_cache.items():
            signals_with_scores[symbol] = generate_signals(sig, params)

        try:
            result = backtest_strategy(signals_with_scores, params)
            if result['total_trades'] < 3:
                continue

            score = (result['total_return_pct'] * 0.3 +
                    result['win_rate'] * 0.2 +
                    result['profit_factor'] * 0.3 +
                    (abs(result['max_drawdown_pct']) * -0.2))

            results.append({**params, **result, 'score': round(score, 2)})

            if score > best_score:
                best_score = score
                best_params = params.copy()

            if count % 50 == 0:
                print(f"  Tested {count}/{total_combos} combinations...")

        except Exception as e:
            continue

    if not results:
        print("No valid results found!")
        return pd.DataFrame()

    results_df = pd.DataFrame(results)
    results_df = results_df.sort_values('score', ascending=False)

    print(f"\n{'='*60}")
    print(f"OPTIMIZATION COMPLETE - Tested {count} combinations")
    print(f"{'='*60}\n")

    print("TOP 5 PARAMETER SETS:")
    print("-" * 120)
    for i, row in results_df.head(5).iterrows():
        print(f"Score: {row['score']:.2f} | Return: {row['total_return_pct']:.2f}% | Win Rate: {row['win_rate']:.2f}% | "
              f"Profit Factor: {row['profit_factor']:.2f} | Max DD: {row['max_drawdown_pct']:.2f}% | "
              f"Trades: {row['total_trades']}")

    return results_df

if __name__ == "__main__":
    test_symbols = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'JPM', 'V', 'JNJ',
                   'WMT', 'PG', 'MA', 'UNH', 'HD', 'DIS', 'BAC', 'XOM', 'SPY', 'QQQ']

    data = fetch_historical_data(test_symbols, days=180)

    if not data:
        print("No data fetched. Check API keys.")
        sys.exit(1)

    results = optimize_parameters(data)

    if not results.empty:
        results.to_csv("backtest_results.csv", index=False)
        print(f"\nResults saved to backtest_results.csv")

        best = results.iloc[0]
        print(f"\n{'='*60}")
        print(f"BEST STRATEGY SUMMARY")
        print(f"{'='*60}")
        print(f"Total Return: {best['total_return_pct']:.2f}%")
        print(f"Win Rate: {best['win_rate']:.2f}%")
        print(f"Profit Factor: {best['profit_factor']:.2f}")
        print(f"Max Drawdown: {best['max_drawdown_pct']:.2f}%")
        print(f"Total Trades: {int(best['total_trades'])}")
        print(f"Final Equity: ${best['final_equity']:.2f}")
