# SESSION STATE
## Last Updated: 2026-05-05

## Server Access
- DigitalOcean IP: [REDACTED_IP]
- Root password: [REDACTED]
- Hostname: trading-bot

## Quick SSH
```bash
sshpass -p '[REDACTED]' ssh -o StrictHostKeyChecking=no root@[REDACTED_IP] "command"
sshpass -p '[REDACTED]' scp -o StrictHostKeyChecking=no /local/file root@[REDACTED_IP]:/remote/path
```

## Project: Trading Bot
- Local: /home/[USER]/trading-agent/
- Remote: /root/trading-agent/
- Container: trading-agent (Docker, running on DO droplet)

## Watchlist (Fully Dynamic Discovery)
- Every 1 hour, bot scrapes Yahoo gainers/losers/most-active + MarketWatch movers
- Picks top 100 trending/volatile stocks
- No static stock universe
- Favors volatile stocks for 5-day holds

## Strategy (Active - Graded Scoring)
- RSI 35/65, MACD, BB, Trend, Momentum
- Min Buy Score: 0.5, Min Sell Score: 1.0
- Stop Loss: -3%, Daily Loss Limit: -5%
- Max 5 trades/day, 10% max position size
- Max hold: 5 days (force-sell stale positions)

## Daily Summary (Fixed)
- Portfolio value based on $200 simulation only:
  `current_value = $200 + realized_pnl + unrealized_pnl`
- Not the total Alpaca account balance

## Backtest Results
- 24 trades, +10.82% gross return, 50% win rate
- +6.18% edge vs SPY
- ~1.3 trades/week

## Live Day 1 (May 4, 2026)
- 5 trades executed: AMZN, DHR, SPY, NVDA, GOOGL
- Quantity capping worked correctly
- Daily summary pending daily

## Config (.env)
- Alpaca Paper API: PKMKUGJJ52VYMSGYXFMIFW7H7N / BJThnbwK2SrnyEFqyk1EwkemQFELBfZtergxx6cFbVq5
- LLM: OpenRouter (Claude 3.5 Haiku)
- ntfy.sh topic: trading-anil-2026

## Docker Commands
```bash
cd /root/trading-agent
docker compose up -d --build   # rebuild & start
docker compose logs -f          # view logs
docker compose down             # stop
docker compose restart          # restart
```

## Key Changes Made This Session
1. Deployed bot to DigitalOcean droplet ([REDACTED_IP])
2. Fixed daily summary: uses $200 simulation P&L, not total account value
3. Added max holding period (5 days) + stale position force-sell
4. Made stock discovery fully dynamic (scrapes trending stocks every hour)
5. Updated universe to favor volatile stocks (semiconductors, AI, biotech, fintech, crypto)
6. Enabled power button → suspend (was masked/ignore)
7. XHCI (USB) set for S3 wakeup

## Resume Instructions
1. Open opencode from /home/[USER]/trading-agent/
2. Run: `sshpass -p '[REDACTED]' ssh root@[REDACTED_IP] "docker compose logs -f"`
3. Check the bot is running and wait for next trading cycle
4. If bot needs restart: `ssh root@[REDACTED_IP] "cd /root/trading-agent && docker compose down && docker compose up -d"`
5. To push code changes: `tar cf - trader/ Dockerfile docker-compose.yml .env requirements.txt | ssh root@[REDACTED_IP] "tar xf - -C /root/trading-agent"` then rebuild
