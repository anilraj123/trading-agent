# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

One LLM-driven trading system, `trader_v2/`, running against an Alpaca **live (real-money) cash account** — the full account, stocks + long options (Level 2).

> ⚠️ **This is a real-money account** (`ALPACA_BASE_URL=https://api.alpaca.markets`), not paper. All P&L is real dollars — treat sizing, deployment, and any "just restart it" action accordingly.

Design: the LLM is a **nightly analyst** (Sonnet) producing structured theses `{symbol, direction, conviction, entry_zone, invalidation, catalyst, instrument, TTL}`; a **deterministic executor** trades the plan on a 15-min cycle. The LLM never touches order flow. The analyst requests the expression per thesis (`shares` | `option`); deterministic gates (conviction ≥ `V2_OPT_MIN_CONVICTION`, account options level, premium sleeve, contract filters) can downgrade an option request to shares (bullish) or skip (bearish — long puts are the only bearish expression, no shorting). A Friday Opus deep-review curates the `lessons.md` memory fed into every research prompt.

Retired systems (cutover 2026-08-21, see `CUTOVER.md`): the v1 equity bot (`trader/` — its service is gone but the package REMAINS as a shared library: `config`, `alpaca_client`, `llm_engine`, `notifications`, `stock_discovery`, `tracker`, `technical_analysis`, `risk_manager.in_close_window` are all imported by trader_v2) and `options_bot/` (deleted).

## Running

Production runs in Docker via `docker-compose.yml` — a single `trader-v2` service. Deployment target is a DigitalOcean droplet at `/root/trading-agent/` (`ssh root@143.110.140.3`).

```bash
# Local dev
source venv/bin/activate
python -m trader_v2                  # daemon
python -m trader_v2 --cycle-once     # one executor cycle, exit
python -m trader_v2 --research-now   # run nightly research once, exit
python -m pytest tests/ -q           # unit tests (pure logic, no network)

# Docker (droplet)
docker compose up -d --build
docker compose logs -f trader-v2
```

Live state lives under `data/v2live/` (`active_theses.json`, `journal.jsonl` — the system of record, `lessons.md`, `run_state.json`, `baseline.json`, `daily_history.csv`). The old paper-run state is preserved in `data/v2/`; v1's ledger in `data/*.csv`. The container mounts `./data:/app/data`; `DATA_DIR` env selects the subdir.

## Architecture notes that aren't obvious from one file

**Capital & cost basis.** `V2_CAPITAL_MODE=dynamic`: sizing and the options sleeve anchor to live account equity each cycle. Lifetime P&L = current equity − **$1,350** (the initial seed; `deposits.csv` empty). v2's own `baseline.json` (equity at cutover) anchors its daily summary + SPY alpha benchmark.

**Thesis lifecycle** (`trader_v2/thesis.py`, pure + unit-tested): `active → entered → closed`, or `active → expired/cancelled`. Nightly research REPLACES all unfilled theses; entered positions get keep/close/revise decisions. The TTL clock restarts at fill. Exits, in precedence order: disaster stop (−8%, every cycle) > research close > invalidation (**close window only** — v1's hard-won lesson: intraday stops on daily-horizon signals went 0-for-11) > trailing (arms at +3%, −3% from HWM) > TTL. Options instead use DTE-tiered premium stops, hard take-profit (+60%), expiry force-exit, and invalidation on the *underlying*.

**Cash account facts.** Multiplier 1 → PDT guard (in `guards.py`) is dormant. Settled-funds bind: buying with unsettled proceeds is allowed; same/next-day selling of those buys risks a GFV (not tracked — accepted). Options Level 2 = long calls/puts only; spreads need the user to request Level 3 in the Alpaca dashboard.

**Fail-safes.** Startup: `V2_REQUIRE_PAPER` guard (live needs the explicit `false` in compose), loud `*** LIVE TRADING ***` banner via ntfy. Every cycle: broker reconcile by broker-symbol (OSI contract for options) — missing positions close their thesis, untracked ones are never touched. −5% daily circuit breaker halts entries. `V2_TRADING_ENABLED=false` is the kill switch (keeps reconcile alive, blocks all trading).

**TA runs on daily bars** for the nightly candidate screen (`get_bars_batch` `days=250, timeframe=1Day`); RSI(14)/MACD/SMA semantics are daily. Stock discovery scrapes Yahoo/Finviz/MarketWatch with a hardcoded fallback universe.

**LLM calls**: `V2_RESEARCH_MODEL` (Sonnet) nightly + post-mortem; `V2_WEEKLY_MODEL` (Opus) Friday review. Sonnet 5 rejects non-default sampling params — every call site passes `temperature=None`. Prompt+response dumps land in `llm_reports/` via `log_llm_call`.

## Files worth knowing

- `PARAMETERS.md` — authoritative table of every tunable, with a changelog. Update it when you change a threshold.
- `CUTOVER.md` — what the 2026-08-21 live cutover changed, droplet steps, rollback.
- `STATUS.md` — session-handoff doc (untracked), updated at the end of every session.
- `.env` — all secrets and runtime config (`.env.example` shows the shape). The droplet's `.env` can override compose-listed defaults — verify effective values in the running container.
- `tests/` — pure-logic tests (thesis validation, gates, options math, guards). No network. Keep them green.

## Conventions

- Don't add a parameter without also adding it to `PARAMETERS.md` and `trader_v2/config.py` with an env-var fallback.
- Every meaningful executor/research decision must be journaled (`store.journal`) — silent no-trades are bugs.
- Market-hours scheduling is `schedule.every(N).minutes` + Alpaca clock short-circuit; nightly research triggers off the open→closed clock edge (+ startup catch-up), never wall-clock.

## Git

This repo lives at `/home/anilraj/code/ai/trading-agent/` on the dev machine. All code changes go through a feature branch off `main` + PR — never commit to `main` directly. Deploy = merge to main, then `git pull` + `docker compose up -d --build` on the droplet.
