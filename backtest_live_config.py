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
from trader.config import Config
from trader.technical_analysis import TechnicalAnalysis

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

TAX_RATE = 0.27
INITIAL_CAPITAL = Config.SIMULATED_ACCOUNT_SIZE

print("="*90)
print("FULL BACKTEST — WALK-FORWARD (LIVE CONFIG)")
print(f"Period: 180 days | Starting Capital: ${INITIAL_CAPITAL:.0f}")
print(f"RSI: {Config.TA_RSI_OVERSOLD}/{Config.TA_RSI_OVERBOUGHT} | "
      f"Min Buy: {Config.TA_MIN_BUY_SCORE} | "
      f"Stop Loss: {Config.TA_STOP_LOSS_PCT:.0%}")
print(f"Volume Threshold: {Config.TA_VOL_THRESHOLD}x | Boost: {Config.TA_VOL_BOOST}x | "
      f"BB: {Config.TA_BB_LOWER_THRESHOLD}/{Config.TA_BB_UPPER_THRESHOLD}")
print("="*90)

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

all_dates = sorted(set().union(*[df.index for df in data.values()]))

def compute_ta_at(df_up_to_date):
    """Compute TA on data up to a specific point (walk-forward)."""
    if len(df_up_to_date) < 30:
        return None

    close = df_up_to_date['close']
    high = df_up_to_date['high']
    low = df_up_to_date['low']
    volume = df_up_to_date['volume']

    rsi_14 = TechnicalAnalysis.rsi(close, 14)
    macd_data = TechnicalAnalysis.macd(close)
    sma_10 = TechnicalAnalysis.sma(close, 10)
    sma_20 = TechnicalAnalysis.sma(close, 20)
    sma_50 = TechnicalAnalysis.sma(close, 50) if len(close) >= 50 else None
    bb = TechnicalAnalysis.bollinger_bands(close)
    atr = TechnicalAnalysis.atr(high, low, close, 14)

    price_change_pct = ((close.iloc[-1] / close.iloc[-2] - 1) * 100) if len(close) > 1 else 0.0
    vol_data = TechnicalAnalysis.volume_analysis(volume, price_change_pct)
    mom_10 = TechnicalAnalysis.momentum(close, 10)

    ta_dict = {
        "current_price": round(close.iloc[-1], 2),
        "rsi_14": rsi_14,
        "macd": macd_data,
        "sma_10": sma_10,
        "sma_20": sma_20,
        "sma_50": sma_50,
        "bollinger_bands": bb,
        "atr_14": atr,
        "volume": vol_data,
        "momentum_10": mom_10
    }

    ta_dict["score"] = TechnicalAnalysis.score_signals(ta_dict)
    return ta_dict

def run_backtest(data, all_dates, capital=INITIAL_CAPITAL):
    position = None
    trades = []
    warmup = 60

    for date in all_dates:
        buy_candidates = []

        for sym, df in data.items():
            if date not in df.index:
                continue

            idx = df.index.get_loc(date)
            if idx < warmup:
                continue

            df_slice = df.iloc[:idx+1]
            ta = compute_ta_at(df_slice)
            if ta is None:
                continue

            if position and position['sym'] == sym:
                price = ta['current_price']
                stop = position['entry'] * (1 + Config.TA_STOP_LOSS_PCT)
                sell_score = ta['score']['sell_score']
                hit_stop = price <= stop

                if sell_score >= Config.TA_MIN_SELL_SCORE or hit_stop:
                    exit_price = stop if hit_stop else price
                    pnl = (exit_price - position['entry']) / position['entry']
                    capital *= (1 + pnl)
                    trades.append({
                        'sym': sym,
                        'entry': position['entry'],
                        'exit': exit_price,
                        'pnl_pct': round(pnl * 100, 2),
                        'buy_score': position['buy_score'],
                        'sell_score': sell_score,
                        'hit_stop': hit_stop,
                        'win': pnl > 0,
                        'date_in': position['date'],
                        'date_out': date,
                        'hold_days': (date - position['date']).days
                    })
                    position = None

            elif position is None:
                buy_score = ta['score']['buy_score']
                if buy_score >= Config.TA_MIN_BUY_SCORE:
                    buy_candidates.append({
                        'sym': sym,
                        'price': ta['current_price'],
                        'score': buy_score,
                        'date': date
                    })

        if position is None and buy_candidates:
            best = max(buy_candidates, key=lambda x: x['score'])
            position = {
                'sym': best['sym'],
                'entry': best['price'],
                'date': best['date'],
                'buy_score': best['score']
            }

    return capital, trades

final_capital, trades = run_backtest(data, all_dates)

wins = [t for t in trades if t['win']]
losses = [t for t in trades if not t['win']]

total_return = ((final_capital / INITIAL_CAPITAL) - 1) * 100
avg_hold = np.mean([t['hold_days'] for t in trades]) if trades else 0
avg_win = np.mean([t['pnl_pct'] for t in wins]) if wins else 0
avg_loss = np.mean([t['pnl_pct'] for t in losses]) if losses else 0

max_drawdown = 0
peak = INITIAL_CAPITAL
running = INITIAL_CAPITAL
for t in trades:
    running *= (1 + t['pnl_pct'] / 100)
    if running > peak:
        peak = running
    dd = (peak - running) / peak * 100
    if dd > max_drawdown:
        max_drawdown = dd

sharpe = 0
if trades:
    returns = [t['pnl_pct'] for t in trades]
    if np.std(returns) > 0:
        sharpe = np.mean(returns) / np.std(returns)

print(f"\n{'='*90}")
print("RESULTS")
print(f"{'='*90}")
print(f"{'Total Trades:':<30} {len(trades)}")
print(f"{'Win Rate:':<30} {len(wins)/len(trades)*100:.1f}% ({len(wins)}W / {len(losses)}L)")
print(f"{'Avg Hold:':<30} {avg_hold:.1f} days")
print(f"{'Avg Win:':<30} {avg_win:+.2f}%")
print(f"{'Avg Loss:':<30} {avg_loss:+.2f}%")
print(f"{'Gross Return:':<30} {total_return:+.2f}%")
print(f"{'Max Drawdown:':<30} {max_drawdown:.2f}%")
print(f"{'Sharpe Ratio:':<30} {sharpe:.2f}")

print(f"\n{'='*90}")
print("DETAILED TRADE LOG")
print(f"{'='*90}")
print(f"{'#':<3} {'Date In':<12} {'Date Out':<12} {'Sym':<6} {'Entry$':>7} {'Exit$':>7} {'P&L%':>7} {'B-Score':>8} {'S-Score':>8} {'Days':>5} {'Stop?':>6}")
print("-"*90)

for i, t in enumerate(trades, 1):
    stop_tag = "HIT" if t['hit_stop'] else ""
    print(f"{i:<3} {str(t['date_in'].date()):<12} {str(t['date_out'].date()):<12} {t['sym']:<6} ${t['entry']:>6.2f} ${t['exit']:>6.2f} {t['pnl_pct']:>+6.2f}% {t['buy_score']:>7.2f} {t['sell_score']:>7.2f} {t['hold_days']:>5} {stop_tag:>6}")

print(f"\n{'='*90}")

spy_sym = 'SPY'
if spy_sym in data:
    spy_df = data[spy_sym]
    spy_start = spy_df['close'].iloc[0]
    spy_end = spy_df['close'].iloc[-1]
    spy_return = ((spy_end / spy_start) - 1) * 100
    spy_final = INITIAL_CAPITAL * (1 + spy_return / 100)
    spy_tax = max(0, spy_final - INITIAL_CAPITAL) * TAX_RATE
    spy_after_tax = spy_final - spy_tax
    spy_after_tax_return = ((spy_after_tax / INITIAL_CAPITAL) - 1) * 100

    diff = total_return - spy_after_tax_return
    print(f"\n{'='*90}")
    print("vs S&P 500 BUY-AND-HOLD")
    print(f"{'='*90}")
    print(f"{'S&P 500 Return:':<30} {spy_return:+.2f}% (${INITIAL_CAPITAL:.0f} → ${spy_final:.2f})")
    print(f"{'S&P 500 After Tax:':<30} {spy_after_tax_return:+.2f}% (tax: ${spy_tax:.2f})")
    print(f"{'Our Strategy Gross:':<30} {total_return:+.2f}%")
    print(f"{'Edge vs S&P 500:':<30} {diff:+.2f}%")
    print(f"{'='*90}")

unique_syms = set(t['sym'] for t in trades)
print(f"\nStocks traded: {len(unique_syms)} — {', '.join(sorted(unique_syms))}")
