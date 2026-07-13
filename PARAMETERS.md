# Tunable Parameters

## Stock Bot

### Capital & Risk (`trader/config.py`)

| Parameter | Current | Description |
|---|---|---|
| `RISK_MAX_POSITION_PCT` | **0.10** (10%) | Max single position as fraction of portfolio |
| `RISK_DAILY_LOSS_LIMIT` | **-5.00** (-5%) | Daily loss % that halts trading |
| `RISK_STOP_LOSS_PCT` | **-0.03** (-3%) | Stop-loss % from entry — **evaluated only in the last `RISK_CLOSE_WINDOW_MIN` min of the session** (daily-bar discipline; intraday polling of this stop was 0-for-11 live) |
| `RISK_INTRADAY_STOP_PCT` | **-0.06** (-6%) | Disaster stop evaluated **every cycle**; also the bracket-order stop-leg level for whole-share buys |
| `RISK_CLOSE_WINDOW_MIN` | **20** min | Pre-close window for the regular stop; keep ≥ `TRADING_INTERVAL_MINUTES` + ~5 min cycle runtime so one cycle always lands inside |
| `RISK_MAX_TRADES_PER_DAY` | **16** | Max trades per day (2× `TARGET_POSITIONS`) |
| `RISK_MAX_HOLDING_DAYS` | **3** | Force-sell position after N **trading days** (weekends + market holidays excluded, via Alpaca's calendar) — **skipped for trailing winners** (gain reached `RISK_TRAIL_ACTIVATE_PCT`); losers/flat unchanged |
| `RISK_TRAIL_ACTIVATE_PCT` | **0.03** (+3%) | Unrealized gain at which a position starts trailing (high-water-mark based) and becomes exempt from the holding-day expiry |
| `RISK_TRAIL_STOP_PCT` | **-0.03** (-3%) | Trailing exit: sell when price ≤ hwm × (1 − 3%), evaluated every cycle. Activated winners exit ≈ breakeven or better |
| `RISK_MIN_HOLDING_DAYS` | **1** | Min days before a *voluntary* (LLM) sell is allowed; hard stops & expiry exempt |
| `RISK_MIN_CONFIDENCE` | **0.6** | Minimum LLM confidence to accept a signal |
| `LLM_SKIP_IDLE_CYCLES` | **true** | Skip the LLM scoring call on cycles with no buy candidate above the bar and no position needing a sell review (behaviour-neutral cost saver; deterministic stops/expiry still run every cycle) |
| `LLM_SELL_REVIEW_PNL_PCT` | **-2.5** (%) | A held position at/below this P&L is "approaching stop" and forces an LLM sell review even on an otherwise-idle cycle. Also drives the matching rule in the LLM prompt |

### Equity-Scaled Sizing (`trader/config.py` — formulas, not fixed values)

Book size and per-position $ are **functions of trading capital**, not constants — a fixed count and a hardcoded $ cap break as equity grows (the old `max(50, min(slice, 2000))` clamp would deploy ~$16k of a $1M account). Call sites use `Config.target_positions()` / `max_position_dollars()` / `max_trades_per_day()`.

| Parameter | Current | Description |
|---|---|---|
| `TARGET_POSITIONS` | **12** | Base book size **at the reference capital** (the formula's anchor, not a fixed count) |
| `TARGET_POSITIONS_REF_CAPITAL` | **$1000** | Capital at which the live book = `TARGET_POSITIONS` |
| `TARGET_POSITIONS_PER_10X` | **6** | Names added per 10× capital: `book = TARGET_POSITIONS + 6·log10(cap/ref)` |
| `TARGET_POSITIONS_MIN` / `_MAX` | **5 / 25** | Book clamp (keep `_MAX` ≤ `WATCHLIST_DEPTH` so the book can fill) |
| `POSITION_HARD_CAP_PCT` | **0.25** | No single position > this fraction of trading capital (replaces the $2000 ceiling) |
| `POSITION_MIN_DOLLARS` | **$10** | Per-position floor so tiny accounts can still place an order (replaces the $50 floor) |
| `RISK_MAX_TRADES_MULT` | **2** | Daily trade cap = this × live book size (unless `RISK_MAX_TRADES_PER_DAY` is pinned) |

Resulting book / per-position / trades-day: **$1k → 8 / $125 / 16**, **$10k → 14 / $714 / 28**, **$100k → 20 / $5k / 40**, **$1M → 25 / $40k / 50**.

### Allocation (`trader/__main__.py`)

| Parameter | Current | Description |
|---|---|---|
| `trading_capital_allocation` | **0.60** (60%) | Account fraction for stock trading (remainder to options) |
| `MIN_NOTIONAL` | **$10** | Minimum order notional value |
| `status_interval` | **4** cycles | Heartbeat notification every N cycles |
| `unexpected_change > 5.0` | **$5** | Deposit detection threshold (ignore smaller equity fluctuations) |
| `bars lookback days` | **250** | Days of daily bar data fetched per symbol for TA |
| `min bars required` | **> 60** | Minimum bars needed to compute TA on a symbol |
| `bar timeframe (equity)` | **Daily (1D)** | Timeframe for TA computation; cached once per trading day |
| `bar timeframe (options)` | **5-min** | Options bot uses a separate intraday fetch for RSI pre-filter |

### Technical Analysis (`trader/config.py` + `trader/technical_analysis.py`)

| Parameter | Current | Description |
|---|---|---|
| `TA_RSI_OVERSOLD` | **35** | RSI threshold: below this is a buy signal |
| `TA_RSI_OVERBOUGHT` | **65** | RSI threshold: above this is a sell signal |
| `TA_RSI_WEIGHT` | **1.0** | Weight for RSI component in composite score |
| `TA_MACD_WEIGHT` | **1.0** | Weight for MACD component |
| `TA_BB_WEIGHT` | **1.0** | Weight for Bollinger Bands component |
| `TA_BB_LOWER_THRESHOLD` | **0.10** | BB position below 10th %ile triggers buy grade |
| `TA_BB_UPPER_THRESHOLD` | **0.90** | BB position above 90th %ile triggers sell grade |
| `TA_TREND_WEIGHT` | **1.0** | Weight for SMA trend cross component |
| `TA_MOM_WEIGHT` | **1.0** | Weight for momentum component |
| `TA_MOM_THRESHOLD` | **2.0%** | Momentum % change threshold to trigger scoring (5-bar) |
| `TA_VOL_THRESHOLD` | **1.2x** | Volume ratio above this activates volume boost |
| `TA_VOL_BOOST` | **1.2x** | Score multiplier when volume exceeds threshold |
| `TA_MIN_BUY_SCORE` | **0.65** | Minimum composite score to place a buy order |
| `TA_MIN_SELL_SCORE` | **0.60** | Minimum composite score to place a sell order |
| `TA_RSI_BUY_MAX` | **72** | Entry guard: RSI above this forces `buy_score=0` (no new longs in overbought names). Replaces the old flat RSI>80 cutoff. |
| `TA_BB_BUY_MAX` | **1.0** | Entry guard: Bollinger position above this (i.e. price above the upper band) forces `buy_score=0`. |
| `TA_MAX_DAY_GAIN_PCT` | **10.0** | Entry guard: a name already up more than this % on the day forces `buy_score=0` (no chasing spikes). |
| `TA_STOP_LOSS_PCT` | alias | Deprecated env-var; if set, used as fallback for `RISK_STOP_LOSS_PCT`. Code now reads `RISK_STOP_LOSS_PCT` everywhere. |

| Indicator | Periods | Lookback |
|---|---|---|---|
| RSI | **14** | 14 daily bars |
| MACD | **8 / 21 / 5** | fast / slow / signal on daily bars (~3-week trend) |
| SMA | **10, 20, 50** | daily bars |
| EMA | **12, 26** | daily bars |
| Bollinger Bands | **20** period, **2.0** std dev | daily bars |
| ATR | **14** | daily bars |
| Momentum | **5** | 5-day % change |
| Volume avg window | **20** bars | rolling daily volume average |

### Discovery (`trader/stock_discovery.py`)

| Parameter | Current | Description |
|---|---|---|
| `UNIVERSE_100` | **100** tickers | Core stock pool (hardcoded list, always seeded into discovery) |
| `STOCK_DISCOVERY_COUNT` | **250** | Max stocks scanned per cycle (universe + live trending), was a hardcoded 150 |
| `WATCHLIST_DEPTH` | **50** | Top-N by buy_score kept as buy candidates after ranking (`trader/__main__.py`), was a hardcoded 30 |
| Per-source scrape limit | **50** | Max stocks taken per Yahoo / Marketwatch source |
| HTTP timeout | **10s** | Web scraping timeout |
| Symbol length filter | **≤ 5** | Max ticker symbol length accepted from scrapers |

### Scheduling

| Action | Frequency | File |
|---|---|---|
| Trading cycle | **15 min** | `trader/__main__.py` |
| Stock discovery refresh | **1 hour** | `trader/__main__.py` |

---

## Options Bot

### Capital (`options_bot/__main__.py`)

| Parameter | Current | Description |
|---|---|---|
| `ALLOCATED_PCT` | **0.40** (40%) | Account fraction allocated to options (~$330 at $826 equity) |
| `PER_POSITION_PCT` | **0.20** (20%) | Per-position % of allocated capital (~$66, allows 2-3 positions) |
| `TOTAL_DEPLOYED_PCT` | **0.50** (50%) | Max total of allocated that can be deployed (cap ~$165) |

### Contract Filters (`options_bot/__main__.py`)

| Parameter | Current | Description |
|---|---|---|
| `CONTRACT_DTE_MIN` | **7** | Minimum days to expiration |
| `CONTRACT_DTE_MAX` | **35** | Maximum days to expiration |
| `OPTIONS_WATCHLIST_SIZE` | **50** | Number of stocks scanned each cycle |
| `MAX_OPTION_SPREAD` | **$1.00** | Maximum bid-ask spread |
| `MIN_OPTION_OI` | **50** | Minimum open interest (exit liquidity floor) |

### Exit Rules (`options_bot/__main__.py`)

| DTE Range | Stop Loss | Condition |
|---|---|---|
| ≤ 5 DTE | **-25%** | Tight stop for very short expiry |
| 6-14 DTE | **-40%** | Medium stop |
| > 14 DTE | **-55%** | Wider stop for longer-dated options |
| Unknown DTE | **force close** | Unparseable symbol → alert + exit immediately |
| Take profit | **+50%** | Close at gain target |
| Force exit | DTE ≤ 3 + hour ≥ 15 | Last hour of expiry day |

### OTM Filter

| Condition | Rule |
|---|---|
| DTE < 15 and OTM > 5% | Reject (too far OTM for short expiry) |

### Scheduling

| Action | Frequency |
|---|---|
| Position management | **15 min** |
| Signal scan cycle | **60 min** |
| Watchlist refresh | **60 min** |

---

## Milestone Review (`PARAMETER_REVIEW_CHECKLIST.md`)

| Milestone | Parameter | Current | Planned |
|---|---|---|---|
| $1,500 | `TOTAL_DEPLOYED_PCT` | 0.50 | 0.33 |
| $2,500 | RSI thresholds | 35/65 | 30/70 |
| $5,000 | `PER_POSITION_PCT` (options) | 0.20 | review |
| $5,000 | `RISK_MAX_POSITION_PCT` | 0.10 | review |
| $5,000 | `TA_MIN_BUY_SCORE` / `TA_MIN_SELL_SCORE` | 0.65 / 0.60 | review |
| $5,000 | `RISK_MAX_TRADES_PER_DAY` | 5 | review |
| $5,000 | `RISK_MAX_HOLDING_DAYS` | 3 | review |

## Changelog

| Date | Change |
|---|---|
| Jul 13 | **Trailing stop — winners exempt from the 3-day expiry.** Live fills: avgW/avgL ≈ 1.35 at a 42% win rate ≈ zero expectancy by construction — the `RISK_MAX_HOLDING_DAYS` expiry truncated exactly the winners that must pay for the losers (BKNG +$30.59 showed the tail exists). Now: each position tracks a high-water mark; once gain ≥ `RISK_TRAIL_ACTIVATE_PCT` (+3%) it turns "trailing" — exempt from expiry, exits when price ≤ hwm × (1 + `RISK_TRAIL_STOP_PCT`) (−3%), so activated winners exit ≈ breakeven or better. Trailing is evaluated **every cycle** (it realizes a locked gain, unlike the loss-realizing intraday stop that moved to the close window) — `stop_type=trailing` CSV rows enable a later "trail exit vs day's close" review if it proves too tight. `trailing` is a sticky bool + hwm **persisted in `risk_state.json`** (`position_trail` key — restart-safe, old state files load cleanly, no migration). Losers/flat keep the 3-day expiry. LLM prompt told to let winners run. 17 tests added (80 pass). Needs droplet **rebuild**. |
| Jul 13 | **Stop-loss moved to the close window + new −6% intraday disaster stop.** Live fills since the 6/22 entry guards showed same-day stop-outs were **0-for-11 (−$32.22)** while multi-day holds were +$17.06 at 45.5% win — the −3% stop was polled every 15-min cycle against a *daily-bar* signal, realizing intraday noise. Now: `RISK_STOP_LOSS_PCT` (−3%, unchanged level) is evaluated only in the last `RISK_CLOSE_WINDOW_MIN` (20) min of the session (via Alpaca's clock `next_close` — early-close days handled); a new `RISK_INTRADAY_STOP_PCT` (−6%) disaster stop runs every cycle; bracket stop legs on whole-share buys move −3%→−6% to match. Also fixed: `close_position`/LLM sells now cancel the symbol's resting bracket/OTO stop leg first (a live leg holds the qty and rejected the close), and the LLM can no longer rebuy a symbol force-exited in the same cycle. Trade CSV `stop_type` distinguishes `close_stop` vs `intraday_disaster`. 19 tests added (64 pass). Needs droplet **rebuild**; verify in-container: `RISK_STOP_LOSS_PCT`, `RISK_INTRADAY_STOP_PCT`, `RISK_CLOSE_WINDOW_MIN`. |
| Jun 22 | **Per-position cap now applies to the TOTAL position, not each order.** `max_position_dollars` (≈equity/`TARGET_POSITIONS`, ~$94 at $1.1k) was checked against a single order's cost, ignoring the existing holding — so the LLM topped up the same name every cycle and it grew unbounded (live: GS hit 41% / BKNG 32% of the account = 73% in two names, despite the 25% `POSITION_HARD_CAP_PCT`). Now caps on `held_mv[symbol] + order ≤ max_position_value`: a name at/over its slice is rejected, otherwise the order is sized to the remaining room. Forces the equal-weight 12-name spread the diversification work intended. Existing over-cap names aren't force-sold — they normalize as they exit via stops/expiry/sell signals. 45 tests pass. Needs droplet **rebuild**. |
| Jun 22 | **Fixed the stock-deployment cap anchor — was stranding ~half the account in cash.** `MAX_STOCK_DEPLOYMENT_PCT` (0.95) was applied to *current cash* (`max_stock_deploy = cash × 0.95`), but cash shrinks as the bot buys, so the cap chased `stock_mv` down and they converged at `0.95/1.95 ≈ 49%` of equity — `0.95` silently behaved like a ~49% cap (live: $554 invested / $576 idle, every BUY hitting "stock deployment at cap"). Now anchored to the equity bot's footprint `(cash + stock_mv) × 0.95`, so it deploys ~95% of equity (~$1,074 of $1,131, ~$57 buffer) as intended. This — not the buy bar or `TARGET_POSITIONS` — was the real reason cash sat idle; the diversification funnel (#35) couldn't help because this cap hard-stopped deployment at ~49%. Needs droplet **rebuild**. |
| Jun 22 | **`RISK_MAX_HOLDING_DAYS` now counts trading days, not calendar days.** The force-sell clock was `(now - entry).days` (calendar), so weekends/holidays counted — a Fri buy was "3 days old" by Mon after just 1 trading session. Now `RiskManager.trading_days_between()` uses `np.busday_count` with US market holidays pulled from Alpaca's calendar (`AlpacaClient.get_market_holidays`, refreshed daily, cached; degrades to weekends-only if the fetch fails). E.g. a Fri-6/19 entry → Wed-6/24 exit (3 trading days) instead of Mon. Validated against live Alpaca calendar (caught Juneteenth 6/19 + Memorial Day 5/25). 6 tests added (45 pass). Needs droplet **rebuild**. (`RISK_MIN_HOLDING_DAYS` is left as a calendar-date diff — a 1-day floor where weekend handling is immaterial since cycles only run on sessions.) |
| Jun 19 | **Wider diversification funnel** — to use idle cash (was ~45% uninvested) by holding *more* names rather than lowering the quality bar. `TARGET_POSITIONS` 8 → **12** (base book; ~$95/position at $1.1k, full-deploy ≈ $1,142, daily trade cap auto-scales to 24). Funnel widened so enough names clear the bar to fill the bigger book: `STOCK_DISCOVERY_COUNT` (new) **250** (was hardcoded 150) and `WATCHLIST_DEPTH` (new) **50** (was hardcoded 30). Quality bar (`TA_MIN_BUY_SCORE`) and the new entry guards are unchanged, so diversification grows without re-introducing top-chasing. Needs droplet **rebuild** + droplet `.env` `TARGET_POSITIONS` 8→12. Tests 39 pass. |
| Jun 19 | **Entry over-extension guards** (`TA_RSI_BUY_MAX=72`, `TA_BB_BUY_MAX=1.0`, `TA_MAX_DAY_GAIN_PCT=10`) — `buy_score` had no upside ceiling, so the bot chased momentum tops (bought names already up 7–13% on the day, at/above the upper band, RSI 65–74) then risk-exited at small losses → 6W/15L the week of Jun 9. These guards zero the *buy* score (sell signals untouched) when a name is too extended to open a new long. Replaces the old flat RSI>80 cutoff. Replaying last week: blocks CPRI/W/DHR/TJX/LLY, still allows ZTS/HD/MMM. Requires droplet **rebuild** (code change). Tests added (39 pass). |
| Jun 17 | **Skip-idle-LLM-cycles** (`LLM_SKIP_IDLE_CYCLES=true`) — the LLM scoring call now runs only when a buy candidate clears the bar or a held position needs a sell review (`needs_llm_review()`); otherwise it would only return HOLD. Cuts ~half the API calls with no behaviour change — deterministic stop-losses and expiry exits run every cycle, before/independent of the LLM. `LLM_SELL_REVIEW_PNL_PCT=-2.5` is the near-stop trigger, shared with the prompt. |
| Jun 17 | `LLM_MODEL`: `claude-sonnet-4-20250514` → **`claude-sonnet-4-6`** — old Sonnet 4 was retired by Anthropic (every cycle 404'd → no LLM trades since ~Jun 16). Same Sonnet tier, same cost profile. Also hotfixed on droplet `.env` + recreated container. |
| Jun 17 | `TARGET_POSITIONS`: 6 → **8** — spread the full 100%-stock allocation across more names so less cash sits idle when signals qualify. Per-position size drops ~$190 → ~$169 on a $1.1k account. Set via droplet `.env` override. |
| Jun 17 | `RISK_MAX_TRADES_PER_DAY`: 12 → **16** — re-tied to 2× `TARGET_POSITIONS` (now 8) so the bot can establish all 8 + rotate in one day. Set explicitly in droplet `.env` (not removed) to avoid the stale-image-default trap. |
| Jun 17 | **Equity-scaled sizing** — replaced the hardcoded `max(50, min(slice, 2000))` position clamp (copy-pasted in 5 files, deployed ~$16k of a $1M account) with `Config.max_position_dollars()` / `target_positions()` / `max_trades_per_day()`. Book size, per-position $, and daily trade cap now grow with capital. `TARGET_POSITIONS` is now the base at `TARGET_POSITIONS_REF_CAPITAL`. At current equity behavior is unchanged. Tests updated (26 pass). Requires droplet **rebuild** (code change) + removing the `RISK_MAX_TRADES_PER_DAY` pin to let it scale. |
| May 18 | Removed broken auto-deposit detection (was creating phantom deposits, corrupting P&L) |
| May 18 | Options no-stack: bot skips symbols already held in open positions |
| May 18 | MACD: 12/26/9 → **8/21/5** on minute bars (shorter intraday trend capture) |
| May 18 | Momentum: 10-bar → **5-bar** (more responsive to recent price action) |
| May 18 | Bars lookback: 3 days → **7 days** (more TA data, fewer symbols skipped) |
| May 18 | Options OI: 0 → **50** (exit liquidity floor) |
| May 18 | Options spread: $2.00 → **$1.00** (T3 spread) |
| May 18 | Options per-position: 25% → **20%** (3 positions instead of 2) |
| May 18 | Unknown DTE: -80% stop → **force close** (alert on bad data) |
| May 18 | Options pre-filter bug: dead code after `continue` meant `_has_viable_option` always returned False (bot never opened new positions); outdented OI check + append |
| May 18 | Risk manager `daily_pnl`: was summing trade % values vs $ threshold (loss limit never tripped); now tracks dollars |
| May 18 | Forced exits (stop loss, holding-period expiry) no longer count toward `RISK_MAX_TRADES_PER_DAY` |
| May 18 | Risk-manager positions registered only AFTER successful submit (no more phantom entries) |
| May 18 | Trader position-size soft cap re-anchored to `trading_capital` to match validator |
| May 18 | Initial seed persisted to `initial_seed.txt` so restarts after a deposit don't double-count |
| May 18 | Daily summary `spy_value` now initialized before the SPY-bars conditional (was NameError on fetch failure) |
| May 18 | `RISK_STOP_LOSS_PCT` is the authoritative stop-loss knob; `TA_STOP_LOSS_PCT` kept only as a backwards-compatible env-var alias |
| May 18 | Cash reservation: each bot only sees its allocation slice of cash, preventing inter-bot races |
| **Jun 05** | **Daily timeframe migration: equity bot TA switched from 5-min to 1D bars (250-day fetch, forming bar dropped, once-per-day cache), LLM prompt updated to daily semantics, warmup threshold >60, SPY RSI filter on daily RSI (effectively dormant — acceptable). Options bot unchanged (still 5-min via default param).** |
| Jun 08 | `validate_order` double-allocation bug fixed: it re-applied `TRADING_CAPITAL_ALLOCATION` to an already-allocated `trading_capital`, making `max_position` 0.6× too tight and silently rejecting valid buys (e.g. $56 vs nominal $50 cap). Now uses the same anchor as the `_execute_decisions` soft cap. |
| Jun 08 | `TARGET_POSITIONS`: 10 → **6**. At ~$1.3k equity, 10 positions floored per-position size to ~$50, rejecting the highest-conviction names ($56-58); 6 lifts the cap to ~$73. |
| Jun 08 | Monitoring: paused cycles (daily trade-cap / loss-limit) now emit a `stage=paused` record to `cycle_log.jsonl` so monitoring doesn't go dark after the cap is hit. |
| Jun 08 | `RISK_MAX_TRADES_PER_DAY`: flat 5 → **2× TARGET_POSITIONS** (=12 at 6). The old 5 was sized for 5-min churn and throttled legitimate daily rebalancing (a 6-position rotate = sell 3 + buy 3 = 6 > 5). Forced exits still excluded. `.env` override removed so the tied default governs. |
| Jun 09 | Options signal fix: bot held 100% of cycles because `_get_signal` passed the LLM only a bare ticker list while demanding it verify trend/RSI/IV criteria it had no data for. Now feeds the already-computed per-symbol TA (RSI/MACD-trend/momentum/price) and reframes the prompt — candidates are pre-qualified (daily uptrend + IV≥0.30 + liquid), so the LLM just picks the strongest momentum directional. |
| Jun 09 | **Options bot paused** (lost $175 / ~11% of account in one flat-market day buying doomed cheap far-OTM short-DTE calls; it also has no daily-loss/trade circuit breaker). `docker compose stop options-bot` on the droplet. |
| Jun 10 | **`RISK_MIN_HOLDING_DAYS` added (=1).** Live bot was churning intraday — voluntary LLM sells round-tripped positions in <30 min (DHR, TJX on Jun 10) even though the daily-bar TA is cached once/day, so the sells reacted to price noise against an unchanged signal. The edge was validated on multi-day daily-bar holds, so same-day voluntary exits were never part of the strategy. Now blocks voluntary sells until the next session; hard stops (`RISK_STOP_LOSS_PCT`) and expiry exits are exempt. Also fixes cash-stranding: the churn burned the daily trade cap, then stops freed cash the bot couldn't redeploy. |
| Jun 09 | **100% of capital to stocks.** `TRADING_CAPITAL_ALLOCATION` 0.60 → **1.0** (equity bot now sources its allocation from Config instead of a hardcoded 0.60), and `MAX_STOCK_DEPLOYMENT_PCT` 0.50 → **0.95** (the 50% cash reservation was an options buffer no longer needed). Deposit-exclusion in `account_value` is unchanged, so sizing is still off principal+PnL, not deposited cash. |
