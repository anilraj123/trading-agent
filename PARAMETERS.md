# Tunable Parameters

## Stock Bot

### Capital & Risk (`trader/config.py`)

| Parameter | Current | Description |
|---|---|---|
| `RISK_MAX_POSITION_PCT` | **0.10** (10%) | Max single position as fraction of portfolio |
| `RISK_DAILY_LOSS_LIMIT` | **-5.00** (-5%) | Daily loss % that halts trading |
| `RISK_STOP_LOSS_PCT` | **-0.03** (-3%) | Stop-loss % from entry |
| `RISK_MAX_TRADES_PER_DAY` | **5** | Max trades per day |
| `RISK_MAX_HOLDING_DAYS` | **3** | Force-sell position after N days |
| `RISK_MIN_HOLDING_DAYS` | **1** | Min days before a *voluntary* (LLM) sell is allowed; hard stops & expiry exempt |
| `RISK_MIN_CONFIDENCE` | **0.6** | Minimum LLM confidence to accept a signal |

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
| `UNIVERSE_100` | **100** tickers | Core stock pool (hardcoded list) |
| Final pool size | **150** | Max stocks from discovery + universe combined |
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
