# Session Status

Handoff notes for the next Claude Code session on this repo. Update this file at the end of every session — overwrite stale sections, don't append a growing log.

## Last updated
2026-08-21 evening (thesis-driven instrument choice MERGED + deployed; synthetic option thesis injected)

## Current live state (as of 8/21 close)
- **Equity bot (`trading-agent`, real money):** rough two weeks — $1,031.87, down from $1,097.92 on 8/10 (gave back ~half of the big early-Aug week). This week −$22.41, 4 wins / 18 trades, every day red. Lifetime −$318 vs the $1,350 seed. v1 keeps trading until cutover — do not stop it.
- **Options bot:** still `Exited (137)`, being retired. Never restart it.
- **trader_v2 (paper):** healthy as a stocks trader — ~flat (−$130 since 7/17 launch, sized to $1,350). Book: AMT/KO/PCG entered (KO pending_close, exits Fri open), BMY + SOFI active. Lesson-memory reasoning in the journal is genuinely good (e.g. KO exhaustion close 8/21 +0.73%).

## Options shakeout — was STALLED, unblocked 2026-08-21
**11 days, zero option entries.** Root cause: `choose_instrument` hard-required `catalyst_date`, but the analyst's inputs (TA + headlines) never surface verifiable dated events and it's forbidden to fabricate — every entry journaled `fallback_reason:no_catalyst_date`. Fixes tonight (user-approved: "enable options trading, all thesis driven"):

1. **PR #53 MERGED to main + deployed to droplet trader-v2** (banner verified: `paper | options level 3 | mode: stocks+options | trading ENABLED`): instrument choice is now thesis-driven. Research schema gains `instrument: shares|option` → stored as `requested_instrument`; bearish always = option request. `choose_instrument` gates the request (not_requested → enabled → level → conviction ≥ `V2_OPT_MIN_CONVICTION` → sleeve); dated-catalyst gates removed, `catalyst_date` is supporting-only now. Bearish validation drops the dated-catalyst hard requirement (conviction bar stays). 245 tests pass.
2. **Synthetic thesis injected** per the STATUS contingency: `T20260821-SOFI`, bullish, conviction 4, `requested_instrument=option`, zone 17.8–19.9 (spot 18.90), TTL 5d. Chain pre-verified viable in-container: `SOFI260925C00019000` Δ0.53, 35 DTE, OI 1203, mid $1.12 (~3 contracts within the $337 budget). Entry should fire Friday 8/22 first cycle (~13:40 UTC).

**Shakeout exit criteria (unchanged):** ≥1 option entered AND exited cleanly on paper; journal premium P&L ×100 correct; reconcile clean (OSI contract matched, no untracked spam); no dangling orders. Watch `[V2]` ntfy + `data/v2/journal.jsonl`. The analyst may also now request options organically — watch for `"instrument": "option"` in nightly research output.

## Next steps
- **Verify Friday 8/22:** SOFI option entry (limit at mid, poll 45s), KO research_close exit, PCG/AMT management. Then an option EXIT over the following days (research_close / premium_stop / take_profit all acceptable — the analyst will likely close the synthetic thesis itself at its next nightly).
- **After shakeout passes:** revert `V2_OPT_MIN_CONVICTION=3` in droplet `.env` (marked `# TEMPORARY`), then build **live cutover** (`feat/v2-live-cutover`): compose rewrite (delete trading-agent + options-bot services, trader-v2 → live keys, `V2_REQUIRE_PAPER=false`, `V2_CAPITAL_MODE=dynamic`, `DATA_DIR=/app/data/v2live`), CUTOVER.md runbook, delete `options_bot/`, PARAMETERS/CLAUDE/STATUS rewrite. Manual droplet steps: flatten v1 positions after close, copy `data/v2/lessons.md` → `data/v2live/`, rebuild, verify LIVE banner, confirm rclone backup covers `data/v2live`.
- **Deferred:** spreads via MLEG after ≥2 weeks clean live long-options (paper account is already level 3; LIVE needs the user to request Level 3 in the Alpaca dashboard).
- **Idea (unbuilt):** an earnings-calendar feed in research inputs would let `catalyst_date` be set honestly instead of almost-never.

## How to check live state (droplet)
SSH: `root@143.110.140.3`, app at `/root/trading-agent/`. See [[deploy-config-gotchas]] memory (pre-built image + `.env` override traps). Quick commands:
```
ssh root@143.110.140.3 "cd /root/trading-agent && docker ps -a"
ssh root@143.110.140.3 "cd /root/trading-agent && tail -5 data/daily_history.csv data/v2/daily_history.csv"
ssh root@143.110.140.3 "cd /root/trading-agent && tail -20 data/v2/journal.jsonl"
```

## In-progress work
None uncommitted — main is clean at PR #53's merge (`a14f833`), droplet trader-v2 runs it. Working tree clean apart from this file (STATUS.md intentionally untracked).
