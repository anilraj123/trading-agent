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
| `RISK_MIN_CONFIDENCE` | **0.6** | Minimum LLM confidence to accept a signal |

### Allocation (`trader/__main__.py`)

| Parameter | Current | Description |
|---|---|---|
| `trading_capital_allocation` | **0.60** (60%) | Account fraction for stock trading (remainder to options) |
| `MIN_NOTIONAL` | **$10** | Minimum order notional value |
| `status_interval` | **4** cycles | Heartbeat notification every N cycles |
| `unexpected_change > 5.0` | **$5** | Deposit detection threshold (ignore smaller equity fluctuations) |
| `bars lookback days` | **7** | Days of minute-bar data fetched per symbol for TA |
| `min bars required` | **> 50** | Minimum bars needed to compute TA on a symbol |

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
| `TA_STOP_LOSS_PCT` | **-0.03** (-3%) | Stop loss for order placement |

| Indicator | Periods | Lookback |
|---|---|---|
| RSI | **14** | 14 minute-bars (~14 min) |
| MACD | **8 / 21 / 5** | fast / slow / signal on minute-bars (~21-min trend) |
| SMA | **10, 20, 50** | minute-bars |
| EMA | **12, 26** | minute-bars |
| Bollinger Bands | **20** period, **2.0** std dev | minute-bars |
| ATR | **14** | minute-bars |
| Momentum | **5** | ~5-minute % change |
| Volume avg window | **20** bars | rolling volume average |

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
| May 18 | MACD: 12/26/9 → **8/21/5** on minute bars (shorter intraday trend capture) |
| May 18 | Momentum: 10-bar → **5-bar** (more responsive to recent price action) |
| May 18 | Bars lookback: 3 days → **7 days** (more TA data, fewer symbols skipped) |
| May 18 | Options OI: 0 → **50** (exit liquidity floor) |
| May 18 | Options spread: $2.00 → **$1.00** (T3 spread) |
| May 18 | Options per-position: 25% → **20%** (3 positions instead of 2) |
| May 18 | Unknown DTE: -80% stop → **force close** (alert on bad data) |
