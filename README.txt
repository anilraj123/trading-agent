============================================================
           AI TRADING AGENT - README
============================================================

OVERVIEW
--------
Autonomous stock trading bot powered by LLM (Claude 3.5 Haiku via
OpenRouter), connected to Alpaca paper trading API. Designed for
$200 simulated portfolio with strict risk guardrails.

Strategy: Mean Reversion (Backtested 54 stocks, 180 days)
- Buy when RSI(14) < 25 (oversold)
- Sell when RSI(14) > 60 (take profits early)
- MACD crossovers and Bollinger Bands as confirmation
- Stop loss at -1% from entry
- Dynamic 100-stock universe refreshed hourly from live sources

Backtest Results (180 days, $200 starting):
  Optimized: +42.36% return, 100% win rate, 5 trades, $284.72 final
  Baseline:  +9.05% return, 45% win rate, 20 trades, $218.10 final

Files in this project:
  trader/__main__.py       - Main bot entry point
  trader/config.py          - Settings & environment config
  trader/alpaca_client.py   - Alpaca API wrapper (orders, data, positions)
  trader/llm_engine.py      - AI trading decision engine
  trader/risk_manager.py    - Risk guardrails & trade validation
  trader/technical_analysis.py - RSI, MACD, Bollinger, SMA, momentum, ATR
  trader/stock_discovery.py  - Dynamic 100-stock universe scanner
  .env                      - API keys & configuration (DO NOT SHARE)
  requirements.txt          - Python dependencies
  optimal_params.json       - Backtested optimal parameters
  trading-agent.service     - systemd service file

ARCHITECTURE
------------
[Stock Discovery] -> scans 100+ stocks hourly from Yahoo Finance, MarketWatch
       |
       v
[Technical Analysis] -> computes RSI, MACD, BB, SMA, momentum, ATR, volume
       |
       v
[LLM (OpenRouter)] -> analyzes TA data -> outputs BUY/SELL/HOLD decisions
       |
       v
[Risk Manager]     -> validates against rules -> approves/rejects orders
       |
       v
[Alpaca API]       -> executes approved orders -> paper trading
       |
       v
[Stop Loss Monitor] -> auto-closes positions at -1% from entry

SETUP (Already Done)
--------------------
1. Created Alpaca paper trading account
2. Created OpenRouter account with Claude 3.5 Haiku
3. Installed dependencies in local venv
4. Configured .env with API keys
5. Installed systemd service (runs in background)

OPTIMIZED PARAMETERS (From Backtest)
------------------------------------
  RSI Oversold:     25  (buy below this)
  RSI Overbought:   60  (sell above this - take profits early)
  RSI Weight:       1.5 (primary signal)
  MACD Weight:      0.5 (confirmation only)
  BB Weight:        0.5 (confirmation only)
  Trend Weight:     0.0 (disabled - trend following underperformed)
  Momentum Weight:  0.5 (confirmation only)
  Momentum Thresh:  1.0%
  Volume Mult:      1.0 (disabled - volume didn't improve results)
  Min Buy Score:    1.5 (moderate signal required)
  Min Sell Score:   2.0 (strong signal required - hold winners longer)
  Stop Loss:        -1% (tight stop)

RISK GUARDRAILS
---------------
  Max Position Size:    10% of $200 = $20 max per trade
  Daily Loss Limit:     2.5% of $200 = -$5/day (bot stops if hit)
  Stop Loss:            -1% per position (auto-execute)
  Max Trades/Day:       5
  Confidence Threshold: 60% minimum for any trade

These limits are enforced regardless of the $100k paper balance.
All risk calculations use SIMULATED_ACCOUNT_SIZE=200.

MANAGEMENT COMMANDS
-------------------
  # View live logs (recommended)
  journalctl -u trading-agent -f

  # Check bot status
  systemctl status trading-agent

  # Stop the bot
  sudo systemctl stop trading-agent

  # Start the bot
  sudo systemctl start trading-agent

  # Restart the bot
  sudo systemctl restart trading-agent

  # View last 50 log entries
  journalctl -u trading-agent -n 50

CONFIGURATION (.env)
--------------------
  ALPACA_API_KEY            - Alpaca paper API key
  ALPACA_SECRET_KEY         - Alpaca paper secret key
  ALPACA_BASE_URL           - Paper: https://paper-api.alpaca.markets
                              Live:  https://api.alpaca.markets

  LLM_PROVIDER              - openrouter
  LLM_API_KEY               - OpenRouter API key
  LLM_MODEL                 - anthropic/claude-3.5-haiku

  SIMULATED_ACCOUNT_SIZE    - 200 (risk calculations use this, not paper $)
  RISK_MAX_POSITION_PCT     - 0.10 (10% max per trade)
  RISK_DAILY_LOSS_LIMIT     - -2.50 (-2.5% daily loss stop)
  RISK_MAX_TRADES_PER_DAY   - 5

  TA_RSI_OVERSOLD           - 25 (buy threshold)
  TA_RSI_OVERBOUGHT         - 60 (sell threshold - take profits early)
  TA_RSI_WEIGHT             - 1.5 (RSI signal strength)
  TA_MACD_WEIGHT            - 0.5 (MACD confirmation)
  TA_BB_WEIGHT              - 0.5 (Bollinger confirmation)
  TA_TREND_WEIGHT           - 0.0 (trend disabled)
  TA_MOM_WEIGHT             - 0.5 (momentum confirmation)
  TA_MOM_THRESHOLD          - 1.0 (momentum threshold %)
  TA_VOL_MULTIPLIER         - 1.0 (volume disabled)
  TA_MIN_BUY_SCORE          - 1.5 (min buy signal score)
  TA_MIN_SELL_SCORE         - 2.0 (min sell signal score)
  TA_STOP_LOSS_PCT          - -0.01 (-1% stop loss)

  TRADING_INTERVAL_MINUTES  - 15 (how often bot checks market)

COST ESTIMATE
-------------
  LLM calls: ~$0.002-0.01 per decision
  ~25 decisions/day during market hours
  Daily cost: ~$0.05-0.25
  Monthly cost: ~$1-5

TRADING SCHEDULE
----------------
  The bot runs every 15 minutes but ONLY executes when:
  - Market is open (Mon-Fri, 9:30 AM - 4:00 PM ET)
  - Risk limits haven't been hit
  - LLM confidence is above 60%
  - Technical signals meet minimum score thresholds

STOCK UNIVERSE
--------------
  The bot maintains a dynamic watchlist of up to 100 stocks:
  - Pre-loaded with 99 liquid large/mid-cap stocks
  - Hourly scans Yahoo Finance (gainers, losers, most active)
  - MarketWatch active stocks
  - Merges live discoveries with base universe
  - Always has fallback stocks if discovery fails

IMPORTANT NOTES
---------------
1. This is PAPER TRADING only. No real money is at risk.
2. Do NOT switch to live trading until:
   - You've paper traded for at least 2 weeks
   - Bot shows consistent profitable results
   - You understand all risks involved
3. Monitor the bot daily using journalctl commands above
4. The PC does NOT need to stay on - the service auto-starts on boot
5. Power settings configured: no sleep/suspend on AC power
6. Never invest more than you can afford to lose

GOING LIVE (FUTURE)
-------------------
To switch to real trading:
1. Create a live Alpaca account and fund it
2. Update ALPACA_BASE_URL to https://api.alpaca.markets
3. Replace ALPACA_API_KEY and ALPACA_SECRET_KEY with live keys
4. Start with small amounts and monitor closely
5. sudo systemctl restart trading-agent

TROUBLESHOOTING
---------------
  Bot not trading?
  -> Check if market is open: journalctl -u trading-agent -n 20
  -> Verify API keys are valid in .env

  LLM errors?
  -> Check OpenRouter balance: openrouter.ai/credits
  -> Verify LLM_MODEL exists in OpenRouter docs

  Want to change watchlist?
  -> Edit .env or wait for auto-discovery (hourly refresh)

  Want to change risk limits?
  -> Edit .env RISK_* or TA_* lines, then: sudo systemctl restart trading-agent

BACKTESTING
-----------
  Run quick backtest:
    source venv/bin/activate
    python backtest_quick.py

  Run full optimization:
    python backtest_seq.py

  Results saved to backtest_full_results.csv

============================================================
  Last Updated: 2026-05-02
  Status: Active (Paper Trading)
  Strategy: Mean Reversion (RSI 25/60, Optimized)
  Next Market Open: Monday 9:30 AM ET
============================================================
