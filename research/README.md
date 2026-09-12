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

## ✅ RESOLVED: the survivorship bias is now fixed — and it overturned a finding

`pit_universe.py` + `pit_data.py` build a **point-in-time** universe: index
membership as Wikipedia recorded it at each historical month-end, and bars for
1,213 symbols including the casualties (SIVB, FRC, TWTR...). A name is only
buyable on bars where it was ACTUALLY a member.

Re-running the same strategies on unbiased data (`run_pit.py`):

| strategy | biased (today's index) | **point-in-time** | bias |
|---|---|---|---|
| live config | −71.1% | **−77.2%** | 6 pts |
| live screen, 60-day hold | +311.1% | **−64.9%** | **376 pts** |
| 6-1 momentum | +5451.8% | **+430.8%** | **5021 pts** |

**This refutes finding 3 below as it was originally written.** "The exits do the
damage, the entries are fine" was an artefact: the +311% for the screen with
sane exits was almost entirely survivorship. On unbiased data that same
configuration returns **−64.9%, alpha −337%**. The screen has no edge at ANY
holding period — the entries are not fine, and fixing the exits alone would not
have saved it.

Momentum survives but shrinks **12x**, and its point-in-time alpha is
concentrated in one regime:

    6-1 momentum alpha   2020-21: +269.0   2022-23: -43.1   2024-25: -1.4   2026: +32.0

Two of four periods are negative or flat. The biased walk-forward's "4/4 folds
positive" was the bias talking. There may be an edge here, but it is regime-
dependent and nothing like as strong as it first looked.

### What is still imperfect
299 of 1,513 ever-members are absent from Alpaca's asset list (mostly pre-2019
acquisitions and renames: AET, ABMD, ABC) — listed in `_cache/pit_unavailable.json`.
Those are mostly *benign* exits (acquired at a premium), so the residual bias is
much smaller than the one removed, and points the wrong way for momentum if
anything. Bankruptcies and failures — the ones that matter — ARE included.

## ⚠️ Historical note: the bias, before it was fixed

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

## THE BOTTOM LINE (point-in-time walk-forward, the only test that counts)

Parameters chosen on a train window, scored on a later window never looked at,
in a universe containing the companies the index threw away:

| family | configs | mean OOS alpha | median | folds positive |
|---|---|---|---|---|
| 6-1 / 12-1 momentum | 128 | +15.2% | **−26.4%** | **1/4** |
| live screen (exits swept) | 72 | −22.0% | −20.2% | **1/4** |

Momentum's positive MEAN is one fold: 2026, +149.8% alpha on **16 trades** in
eight months. The other three are −36.4, −33.3, −19.4. The median is −26.4%.

**Neither family has a tradeable edge.** Buying and holding SPY beat every
single configuration tested, in both families, on unbiased data.

### The one thing that replicated everywhere
`invalidation_pct = OFF` was selected in **16/16 fold-selections** — both
families, both universes, biased and point-in-time. In the biased run the
invalidation exit alone was 315 trades at a **0% win rate** for −$22,768. The
live LLM analyst independently reached the same conclusion on 2026-09-11 after
SBAC's −2.39% invalidation exit round-tripped straight back to entry.

That is a real, replicated, loss-REDUCING finding. It does not make the
strategy profitable; it makes it lose less.

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
