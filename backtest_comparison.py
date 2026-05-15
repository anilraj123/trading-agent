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

TAX_RATE = 0.27
INITIAL_CAPITAL = 200.0

print("="*70)
print("STRATEGY vs S&P 500 BUY-AND-HOLD COMPARISON")
print("="*70)
print(f"Period: 180 days | Starting Capital: ${INITIAL_CAPITAL}")
print(f"Short-term tax rate: {TAX_RATE*100:.0f}%\n")

# Fetch data for all stocks + SPY
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

# S&P 500 Buy-and-Hold
spy_df = data['SPY']
spy_start = spy_df['close'].iloc[0]
spy_end = spy_df['close'].iloc[-1]
spy_return = ((spy_end / spy_start) - 1) * 100
spy_final = INITIAL_CAPITAL * (spy_end / spy_start)
spy_tax = (spy_final - INITIAL_CAPITAL) * TAX_RATE
spy_after_tax = spy_final - spy_tax

print(f"{'S&P 500 BUY-AND-HOLD (SPY)':<40}")
print(f"  Start Price:  ${spy_start:.2f}")
print(f"  End Price:    ${spy_end:.2f}")
print(f"  Return:       {spy_return:.2f}%")
print(f"  Final Value:  ${spy_final:.2f}")
print(f"  Tax ({TAX_RATE*100:.0f}%):   -${spy_tax:.2f}")
print(f"  After Tax:    ${spy_after_tax:.2f}\n")

# Compute indicators
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

# Run strategy backtest with optimized params
params = {
    'rsi_period': 14, 'rsi_os': 25, 'rsi_ob': 60, 'rsi_weight': 1.5,
    'macd_fast': 12, 'macd_slow': 26, 'macd_weight': 0.5,
    'bb_period': 20, 'bb_weight': 0.5,
    'sma_short': 20, 'sma_long': 50, 'trend_weight': 0.0,
    'mom_period': 10, 'mom_thresh': 1.0, 'mom_weight': 0.5,
    'vol_period': 20, 'vol_mult': 1.0,
    'min_buy': 1.5, 'min_sell': 2.0, 'stop_loss': -0.01
}

def run_strategy(sig_cache, params, capital=200.0):
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
                'sym': position['sym'],
                'entry': position['entry'],
                'exit': price,
                'pnl': pnl * 100,
                'win': pnl > 0,
                'date_in': position['date'],
                'date_out': date
            })
            position = None

        if action == 'BUY' and position is None:
            price = sig_cache[best_sym].loc[date, 'close']
            position = {'sym': best_sym, 'entry': price, 'date': date}

    # Calculate tax on gains
    total_gain = sum([t['pnl'] for t in trades if t['win']])
    total_loss = sum([abs(t['pnl']) for t in trades if not t['win']])
    net_gain_pct = total_gain - total_loss
    tax_amount = max(0, net_gain_pct / 100 * INITIAL_CAPITAL) * TAX_RATE

    final_value = capital
    after_tax_value = final_value - tax_amount

    return {
        'capital': round(capital, 2),
        'after_tax': round(after_tax_value, 2),
        'tax_paid': round(tax_amount, 2),
        'return': round(((capital / INITIAL_CAPITAL) - 1) * 100, 2),
        'after_tax_return': round(((after_tax_value / INITIAL_CAPITAL) - 1) * 100, 2),
        'trades': len(trades),
        'wins': len([t for t in trades if t['win']]),
        'losses': len([t for t in trades if not t['win']]),
        'trade_details': trades
    }

result = run_strategy(sig_cache, params)

print(f"{'OUR STRATEGY (Mean Reversion)':<40}")
print(f"  Trades:       {result['trades']} ({result['wins']}W / {result['losses']}L)")
print(f"  Return:       {result['return']:.2f}%")
print(f"  Final Value:  ${result['capital']:.2f}")
print(f"  Tax ({TAX_RATE*100:.0f}%):   -${result['tax_paid']:.2f}")
print(f"  After Tax:    ${result['after_tax']:.2f}\n")

print("="*70)
print("FINAL COMPARISON (After 27% Short-Term Tax)")
print("="*70)

spy_profit = spy_after_tax - INITIAL_CAPITAL
our_profit = result['after_tax'] - INITIAL_CAPITAL
outperformance = our_profit - spy_profit

print(f"\n{'Metric':<30} {'S&P 500':>15} {'Our Strategy':>15} {'Diff':>10}")
print("-"*70)
print(f"{'Starting Capital':<30} ${INITIAL_CAPITAL:>14.2f} ${INITIAL_CAPITAL:>14.2f} $0.00")
print(f"{'Final Value':<30} ${spy_after_tax:>14.2f} ${result['after_tax']:>14.2f} ${outperformance:>+.2f}")
print(f"{'Profit':<30} ${spy_profit:>14.2f} ${our_profit:>14.2f} ${outperformance:>+.2f}")
print(f"{'Return %':<30} {((spy_after_tax/INITIAL_CAPITAL)-1)*100:>14.2f}% {result['after_tax_return']:>14.2f}%")
print(f"{'Tax Paid':<30} ${spy_tax:>14.2f} ${result['tax_paid']:>14.2f}")

print(f"\n{'='*70}")
print("DETAILED TRADE LOG")
print(f"{'='*70}")
print(f"{'#':<3} {'Date In':<12} {'Date Out':<12} {'Symbol':<8} {'Entry $':>8} {'Exit $':>8} {'P&L %':>8} {'Tax $':>8} {'Net $':>8}")
print("-"*70)

cumulative_tax = 0
cumulative_net = INITIAL_CAPITAL

for i, trade in enumerate(result['trade_details'], 1):
    pnl_pct = trade['pnl']
    position_value = cumulative_net * 0.10
    gross_profit = position_value * (pnl_pct / 100)
    tax_on_trade = max(0, gross_profit) * TAX_RATE
    net_profit = gross_profit - tax_on_trade
    cumulative_net += gross_profit - tax_on_trade
    cumulative_tax += tax_on_trade

    print(f"{i:<3} {str(trade['date_in'].date()):<12} {str(trade['date_out'].date()):<12} {trade['sym']:<8} ${trade['entry']:>7.2f} ${trade['exit']:>7.2f} {pnl_pct:>+7.2f}% ${tax_on_trade:>7.2f} ${net_profit:>+7.2f}")

print(f"\n{'Total Tax Paid':<50} ${cumulative_tax:>18.2f}")

print(f"\n{'='*70}")
if outperformance > 0:
    print(f"STRATEGY OUTPERFORMS S&P 500 BY ${outperformance:.2f} ({((result['after_tax_return']-((spy_after_tax/INITIAL_CAPITAL)-1)*100)):+.2f}%)")
else:
    print(f"S&P 500 OUTPERFORMS STRATEGY BY ${abs(outperformance):.2f}")
print(f"{'='*70}")
