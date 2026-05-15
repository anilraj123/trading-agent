# AI Trading Agent

Autonomous stock trading bot powered by LLM, supervised via risk guardrails.

## Architecture

```
[LLM (OpenRouter)] → analyzes market data → outputs BUY/SELL/HOLD decisions
       ↓
[Risk Manager] → validates against rules → approves/rejects
       ↓
[Alpaca API] → executes approved orders → paper/live trading
```

## Setup

### 1. Create Alpaca Account
- Go to [alpaca.markets](https://alpaca.markets)
- Sign up for free
- Get API keys from dashboard
- **Start with paper trading** (default in this bot)

### 2. Get LLM API Key
- Go to [openrouter.ai](https://openrouter.ai)
- Sign up, add $5-10 credit
- Create API key
- Recommended model: `anthropic/claude-3.5-haiku` (cheap, good for trading)

### 3. Install & Configure

```bash
cd trading-agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your keys
```

### 4. Run (Paper Trading)

```bash
source venv/bin/activate
python -m trader
```

### 5. Monitor

The bot prints status every cycle. Logs are in terminal output.

## Risk Guardrails

| Rule | Default | Description |
|------|---------|-------------|
| Max Position | 10% of portfolio | Never risk more than $20 on one trade |
| Daily Loss Limit | -$5.00 | Bot stops trading if down $5 in a day |
| Stop Loss | -3% per position | Automatic loss cutoff |
| Max Trades/Day | 5 | Prevents overtrading |

## Configuration (.env)

```env
# Alpaca API (paper or live)
ALPACA_API_KEY=your_key
ALPACA_SECRET_KEY=your_secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# For live trading, change to:
# ALPACA_BASE_URL=https://api.alpaca.markets

# LLM
LLM_PROVIDER=openrouter
LLM_API_KEY=your_key
LLM_MODEL=anthropic/claude-3.5-haiku

# Risk settings
RISK_MAX_POSITION_PCT=0.10
RISK_DAILY_LOSS_LIMIT=-5.00
RISK_STOP_LOSS_PCT=-0.03
RISK_MAX_TRADES_PER_DAY=5

# Trading schedule
TRADING_INTERVAL_MINUTES=15
WATCHLIST=AAPL,MSFT,TSLA,SPY,QQQ
```

## Cost Estimate

- LLM calls: ~$0.002-0.01 per decision
- 15-min intervals = ~25 decisions/day during market hours
- Daily cost: ~$0.05-0.25
- Monthly cost: ~$1-5

## Important Notes

- **Always paper trade first** - minimum 2 weeks before going live
- $200 is small - focus on percentage returns, not dollar amounts
- The bot is autonomous but you should monitor daily
- Never deposit more than you can afford to lose
