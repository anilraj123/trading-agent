# Live Cutover — trader_v2 on the full real-money account

Executed **2026-08-21** (user-directed: "start it on the full money account").
This document records what changed, the manual droplet steps, and the rollback.

## What the cutover is

trader_v2 (thesis-driven stocks+options) becomes the ONLY trading system,
running the **full live account** (`https://api.alpaca.markets`). Retired at
cutover:

- `trading-agent` (v1 equity bot) — service removed from compose; `trader/`
  stays in the repo as a shared library (v2 imports config/client/LLM/TA/
  discovery/tracker from it).
- `options-bot` — service removed AND `options_bot/` deleted from the repo
  (dead since June, −$216 lifetime).

## Compose / config facts (the authority is docker-compose.yml)

| Setting | Value | Why |
|---|---|---|
| `ALPACA_BASE_URL` | `https://api.alpaca.markets` | live, explicit in compose |
| `V2_REQUIRE_PAPER` | `false` | deliberate opt-out of the paper startup guard |
| `V2_CAPITAL_MODE` | `dynamic` | sizing + options sleeve anchor to live equity each cycle |
| `DATA_DIR` | `/app/data/v2live` | fresh state; paper history stays in `data/v2/` |
| Account | CASH, options Level 2 | PDT guard dormant; long calls/puts only (Level 3 = user request in Alpaca dashboard, for spreads later) |

## Manual droplet steps performed 2026-08-21

1. `git pull` on the droplet; removed the `# TEMPORARY` `V2_OPT_MIN_CONVICTION=3`
   shakeout override from `.env` (live uses the default 4).
2. `mkdir data/v2live` and copied `data/v2/lessons.md` → `data/v2live/lessons.md`
   (the paper bot's earned lessons carry over; theses/journal/run-state do NOT —
   live starts a fresh book and baseline).
3. `docker compose up -d --build --remove-orphans` — builds trader-v2 on merged
   main, starts it live, removes the orphaned `trading-agent` and `options-bot`
   containers.
4. Verified the startup banner: `*** LIVE TRADING *** | ... | cash (PDT n/a...) |
   options level 2 | mode: stocks+options | trading ENABLED`.
5. Flattened v1's legacy equity positions: `close_position` for each (fractional
   market orders queue for the next open when submitted after hours). Until they
   fill, v2's reconcile reports them as `untracked` and does not touch them.
6. Backup: `backup-data.sh` on the droplet now execs rclone inside `trader-v2`
   (the old script used the deleted `trading-agent` container); it copies all of
   `/app/data`, so `data/v2live/` is included automatically.

## First-session expectations

- Boot after close → startup catch-up runs the nightly research immediately →
  first live theses same night; first entries the next session.
- v1's flatten orders fill at the open; proceeds settle T+1 (cash account —
  buying with unsettled proceeds is fine; selling those buys before settlement
  risks a good-faith violation, which v2 does not track — acceptable, rare).
- `deposits.csv` / `initial_seed.txt` cost-basis logic was v1's. Lifetime P&L
  remains: current equity − $1,350 seed. v2's own baseline (`data/v2live/
  baseline.json`) anchors its per-run reporting and SPY benchmark.

## Rollback (if v2 must be stopped)

```bash
# stop trading NOW (kill switch, positions stay):
ssh root@143.110.140.3 "cd /root/trading-agent && \
  sed -i 's/^V2_TRADING_ENABLED=.*/V2_TRADING_ENABLED=false/' .env || echo 'V2_TRADING_ENABLED=false' >> .env && \
  docker compose up -d trader-v2"

# full rollback to the pre-cutover world:
git revert <cutover-merge-commit>   # restores 3-service compose + options_bot
# droplet: git pull && docker compose up -d --build
# then manually flatten anything v2 opened.
```

The kill switch (`V2_TRADING_ENABLED=false`) keeps reconcile/journaling alive
but blocks all entries and exits — for a true emergency also flatten positions
manually in the Alpaca dashboard.
