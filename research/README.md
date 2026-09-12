# Strategy research — backtest & walk-forward harness

Run order: `fetch_universe.py` → `python -m research.data` (builds the 66 MB
bar cache) → any of the `run_*`/`sweep_*`/`wf_*` scripts. Local venv:
`venv/bin/python`. `_cache/` is gitignored.

| module | what it is |
|---|---|
| `data.py` | Alpaca daily bars 2016→, 903 S&P 500+400 symbols, parquet cache |
| `engine.py` | the simulator: live screen + live exit rules, `Params` = every knob |
| `metrics.py` | one place that computes returns/Sharpe/DD/expectancy |
| `sweep.py` | multiprocess grid search + `walk_forward` |
| `validate_engine.py` | engine sanity: exits off must ≈ buy-and-hold |
| `control_momentum.py` | harness sanity: can it find edge known to exist? |

## ⚠️ Survivorship bias — read before quoting any number

`universe.json` is **today's** S&P 500+400 membership. Backtesting it over
history excludes every company dropped from the index, so **absolute returns
are an upper bound, not a forecast.** The bias grows with holding period, and
it hits momentum hardest: a strategy that buys past winners inside a universe
selected *for having won* is close to look-ahead.

Comparisons between strategies on the same universe are far more trustworthy
than any strategy's absolute number. Every conclusion below is stated as a
comparison for that reason.

Resolving this needs point-in-time index membership (or a delisting-inclusive
universe). Until then, no number here justifies sizing real money.

## Findings

**1. The engine is sound.** Exits disabled → +189% (screen) / +348% (no
filter) over 2016-2026, against SPY +272%. A simulator bug would not land in
the market's ballpark.

**2. The deployed configuration loses catastrophically.** −71.5% over the same
window, −81% max drawdown, 2,244 trades, profit factor 0.87, expectancy
−0.174%/trade. SPY made +272%.

**3. The exits, not the entries, do the damage.** Identical entries:

| | return |
|---|---|
| live screen, 60-day hold, no stops | **+311%** |
| live screen, live exits | **−71%** |

Same names. The exit system destroys ~380 points.

**4. There is no gross edge to begin with.** At `cost_bps=0` the live config
returns −13% with profit factor 0.99 and expectancy +0.025%/trade — a coin
flip before costs. Then 2,244 round trips at ~5 bps of equity each guarantee
the loss. Turnover is the mechanism; the absent edge is the cause.

**5. Exit findings that DO replicate.** Across 8 independent walk-forward fold
selections, `invalidation OFF` was chosen 8/8 and TTL ≥ 40 days 8/8. The −5%
invalidation and the 5-day TTL are both actively harmful.

**6. But tuning the screen does not rescue it.** Walk-forward, picking on
train and scoring on unseen test: **0/4 folds beat SPY** (objective Sharpe,
mean alpha −32%); 1/4 (objective return, mean alpha −14%). The in-sample
optima do not generalise.

**7. Momentum does replicate — with the bias caveat in full force.** 6-1
momentum, 8 positions, 40-120 day holds, no stops: **4/4 folds positive alpha
out of sample**, and all four folds independently selected the same
parameters. Against that, `lowvol` ranking produced ~0 alpha under identical
conditions, so the harness is not simply rewarding everything.

**The honest reading of 7:** the *ordering* (momentum ≫ current screen ≫
nothing) is credible. The *magnitude* is not, and must not be traded on until
the universe is point-in-time.
