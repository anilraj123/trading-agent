# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Two independent LLM-driven trading bots running side-by-side against an Alpaca paper account, each with its own capital allocation slice:

- `trader/` — equity day-trader, 60% of account, 15-min cycle, RSI/MACD/BB technical scoring
- `options_bot/` — long calls/puts trader, 40% of account, 60-min signal cycle + 15-min position management

Both share `trader/config.py`, `trader/alpaca_client.py`, `trader/llm_engine.py`, `trader/notifications.py`, `trader/stock_discovery.py`, and `trader/tracker.py`. The options bot imports from `trader/` rather than duplicating.

## Running

Production runs in Docker via `docker-compose.yml` — two services (`trading-agent`, `options-bot`) from the same image, sharing `./data` and `.env`. Deployment target is a DigitalOcean droplet at `/root/trading-agent/` (see `SETUP_DIGITALOCEAN.md`).

```bash
# Local dev
source venv/bin/activate
python -m trader        # equity bot
python -m options_bot   # options bot (uses trader.* modules)

# Docker (preferred on droplet)
docker compose up -d --build
docker compose logs -f trading-agent
docker compose logs -f options-bot
docker compose restart options-bot
```

`trader/tracker.py` and `options_bot/__main__.py` hardcode `/app/data` as the data dir — they expect the Docker volume mount. Running locally outside Docker will fail to write CSVs unless `/app/data` exists.

Tests: none. The "backtest_*.py" scripts at the repo root are exploratory/research, not CI. `backtest_live_config.py` replays the live strategy against historical bars.

## Architecture notes that aren't obvious from one file

**Capital accounting decouples Alpaca equity from trading capital.** `TradingBot.__init__` reads `deposits.csv` (manually maintained list of cash injections) and subtracts the sum from current equity to compute `self.account_value`. Then `trading_capital = account_value * trading_capital_allocation` (currently 0.60). All risk-manager position sizing uses this derived `trading_capital`, NOT raw Alpaca equity. The options bot does the parallel calculation with `ALLOCATED_PCT = 0.40`. When you change allocations make sure both bots' percentages still sum to ≤ 1.0 and that you understand the deposit-tracking is fragile (the old auto-detect was removed for creating phantom deposits — see `PARAMETERS.md` changelog).

**TA runs on minute bars, not daily.** Despite the bot being described elsewhere as a swing trader, `TechnicalAnalysis.compute_all` is fed `alpaca.get_bars(symbol, days=7)` which returns minute bars. The "RSI(14)" is 14 minutes of price action, "MACD(8/21/5)" is sub-30-minute trend, "momentum(5)" is 5 minutes. The LLM prompt in `trader/llm_engine.py` explicitly states this — don't conflate the indicator name with daily-bar semantics.

**Risk-manager state is in-memory and resets on restart.** `RiskManager.daily_trades`, `positions`, `trade_log` all live in process memory. A container restart loses today's trade count and position-entry timestamps (so `RISK_MAX_HOLDING_DAYS` clock resets too). Persistent ledger is CSVs in `/app/data/` written by `trader/tracker.py`, but those are append-only logs, not reload sources.

**Two ordering paths for stop losses.** `submit_market_order` uses an Alpaca bracket order when quantity is a whole number, falling back to a "software stop" (tracked in `RiskManager.positions`, polled each cycle via `check_stop_losses`) for fractional shares. Both paths exist intentionally — fractional-share bracket orders aren't supported by Alpaca.

**Options bot has dynamic stop-loss tiers by DTE** (`_get_dynamic_stop` in `options_bot/__main__.py`): -25% if ≤5 DTE, -40% if 6–14, -55% if >14. Plus a force-close in the last hour of the expiry day if DTE ≤ 3. Unknown/unparseable DTE → immediate force exit. The DTE is parsed from the option symbol itself (`symbol[-15:-9]`) — OSI format.

**LLM provider switching.** `Config.LLM_PROVIDER` selects between `anthropic` (uses `ANTHROPIC_API_KEY`) and `openrouter` (uses `LLM_API_KEY`). The model name in `LLM_MODEL` must match the provider's namespace.

**Stock discovery scrapes the web.** `trader/stock_discovery.py` hits Yahoo Finance gainers/losers/most-active and MarketWatch hourly. Failures fall back to a hardcoded `UNIVERSE_100` list. The options bot reuses the same scraper but takes the first 50.

## Files worth knowing

- `PARAMETERS.md` — authoritative table of every tunable, with a changelog. Update it when you change a threshold.
- `PARAMETER_REVIEW_CHECKLIST.md` — equity-milestone-gated config reviews (e.g., revisit `TOTAL_DEPLOYED_PCT` at $1500).
- `.env` — all secrets and runtime config. `.env.example` shows the expected shape.
- `data/daily_history.csv`, `trade_log.csv`, `stock_discovery.csv` — append-only CSVs written by `trader/tracker.py`. `llm_reports/` holds per-cycle prompt+response dumps if email is enabled.
- `deposits.csv` (under the data volume) — manually maintained list of cash injections; load order matters for P&L math.

## Conventions

- Don't add a parameter without also adding it to `PARAMETERS.md` and (if user-tunable) to `trader/config.py` with an env-var fallback.
- The bots talk to the same Alpaca account. Anything that reads positions must distinguish equity (`len(p.symbol) <= 10`) from options (`> 10`) — the options bot already does this; don't break it from the equity side.
- Both bots assume market-hours scheduling via `schedule.every(N).minutes.do(...)` and short-circuit on `alpaca.get_market_status()`. Don't move to a fixed cron unless you also handle the market-closed case.
