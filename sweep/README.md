# sweep — cache-then-search split for backtest_5min_sweep.py

The original script does three things in one process: fetch 54 symbols of 5-min bars
from Alpaca, precompute TA indicators into per-bar buy/sell scores, then sweep 5
thresholds. Only the third part varies per config, so fanning the whole script out
would re-fetch (Alpaca rate-limits per *account*, not per machine) and redo the
precompute for every point in the grid.

    cache.py   fetch + precompute ONCE  -> cache.npz   (needs venv: pandas, alpaca, .env)
    run.py     one or many configs      -> JSON lines  (needs numpy only)
    verify.py  proves run.py == original algorithm

## Why cache.py execs the original

`cache.py` reads `backtest_5min_sweep.py`, splits its source at
`all_times = sorted(all_times)`, and `exec`s the prefix. The fetch and the ~90 lines
of TA scoring are therefore *the original's own code*, not a transcription — they
cannot silently drift, and edits to the original are picked up automatically.

The cache is dense `[time x symbol]` matrices: `priceM`, `sellM`, `buyM`, `hasbar`.
54 symbols x 2688 bars, **0.5 MB compressed** — small enough that shipping it to a
node costs ~20 ms at the fleet's ~220 Mbit/s.

`buyM` holds `-inf` where the bar is missing or `idx < WARMUP`, so it can never win
an argmax and never clears a positive threshold. That is what lets the per-bar
candidate scan become one vectorised row lookup.

## Correctness

`verify.py` runs a literal transcription of the original `run()` loop — per-symbol
scan, `max(candidates, key=...)` — against `run.py`'s vectorised version on the same
cache, and compares **trade for trade**, not just summary stats:

    8/8 configs identical (trade-for-trade)

covering the original's 5 thresholds plus 3 off-diagonal configs. The tie-break
matters and is preserved: `np.argmax` returns the first maximum, and the matrix
columns are in the original's dict-insertion order, so ties resolve exactly as
`max()` resolved them.

## Performance — read this before farming it

Per-config simulation is **~0.008 s**. A single `run.py` invocation costs ~0.1 s,
almost all of it loading the npz; SSH to a node costs **~0.5 s**. So:

| grid | MSI alone (6 slots) | fleet (18 slots) | |
|---|---|---|---|
| 2,400 configs, 50/task | 2.1 s | 1.9 s | no gain |
| 48,000 configs, 1000/task | 25.3 s | **15.0 s** | 1.69x |

**Distributing 2,400 configs is pointless.** The crossover sits between the two rows:
the fleet starts paying once total serial work is tens of seconds, which for this
workload means tens of thousands of configs batched ~1000 per task.

Work self-balances by machine speed, so slot counts need no per-node tuning — on the
48k run the queue handed out 23 tasks to the MSI, 16 to the IdeaPad 5 and only 9 to
the slower HP, each ending up ~75 s busy. The task must be coarse relative to
the ~0.5 s SSH setup, which means batching thousands of configs per task, which in
turn means the whole grid has to be large before the fleet wins anything.

Two further cautions specific to this sweep:

- **The grid is over-resolved.** Scores are rounded to 2 dp, so adjacent thresholds
  produce *identical trade sets* — `buy=2.067` and `buy=2.378` returned the same 184
  trades. The number of distinct outcomes is far below the nominal config count.
- **Best-of-N on one 20-day window is selection noise.** The original's 5 thresholds
  all lost (-13.8% to -6.7%); widening to 2,400 configs surfaced +7.85%. That is what
  searching harder on a fixed history does. Walk-forward with out-of-sample folds is
  the thing worth spending compute on — not more parameters on the same window.

## Walk-forward (`wf.py`)

`backtest_daily_walkforward.py` fixes `THRESHOLD = 1.0` and reports the same
parameters across three windows, so every number it prints is **in-sample** — the
windows test robustness of one config, not of the selection process. `wf.py` does the
other thing: for each fold it searches the grid on TRAIN, freezes the winner, and
evaluates it once on an unseen TEST window.

    ./wf.py --cache cache90.npz --train 0:6000 --test 6000:7000 --objective sharpe

- `cache.py --days 90` widens the fetch (11,904 bars x 54 symbols, 2.4 MB) so folds
  can be disjoint. It rewrites `DAYS = <n>` in the original's source before exec'ing
  it, so the precompute is still the original's own code.
- `simulate(..., lo, hi)` restricts a run to a bar range. A fold starts flat, matching
  how `run_backtest` is called on a time subset. Verified: slicing the full range is
  bit-identical to not slicing, and the 8/8 trade-for-trade check still passes.
- `--min-trades` (default 20) drops train configs with too few trades — a 3-trade
  config with a great Sharpe is noise, and without this the search reliably picks it.
- Ties keep the FIRST config, via strict `>` in the selection loop.

**This is the workload the fleet was worth building for.** One fold is a full grid
search — ~7.2 s for 1,200 configs — so SSH setup is ~7% overhead instead of the 500%
it was for a single config. Folds are independent and retryable, so a laptop
disappearing mid-fold costs one fold, not the run.

Read the `test_metrics`, not `train_metrics`. The train row is the best of 1,200
configs on that window and is optimistic by construction; the gap between the two is
the honest measure of how much of the edge was fitting.

### Measured

| 28 folds (4000-bar train, 1000-bar test, step 250) | wall clock |
|---|---|
| MSI alone (6 slots) | 27.1 s |
| fleet (18 slots) | **17.7 s** |

1.53x. Distribution: MSI 12 folds, IdeaPad 5 10, HP 6 — the queue again balanced
itself by machine speed without any per-node tuning.

### Result

    mean TRAIN return +12.326%    mean TEST (out-of-sample) -0.912%
    median TEST -1.028%           folds with TEST > 0: 6/28

The 12-point gap between train and test is the overfit, measured. Best-of-1,200 on
each train window averages +12.3%; those same parameters average -0.9% on the very
next unseen window, and lose in 22 of 28 folds. The parameters are also unstable —
the winner jumps between (3.0, 2.378) and (2.689, 2.378) and (3.0, 1.444) as the
window slides, which is what fitting noise looks like.

Three caveats, all of which make the real picture *worse*, not better:

- **No transaction costs.** `backtest_5min_sweep.py`'s `run()` compounds raw price
  moves; there is no commission or slippage anywhere in it (unlike
  `backtest_daily_walkforward.py`, which sets `COST_BPS = 0.0005`). At 5 bps
  round-trip, the 852-trade configs would shed ~40 points of return on costs alone.
- **Tiny test samples.** 1-11 trades per test fold. Individual folds are noise; only
  the aggregate says anything.
- **Overlapping folds.** Step 250 against a 4000-bar train window means consecutive
  folds share most of their data, so the 28 results are not 28 independent samples.

The pipeline is sound and the fleet does real work here. The strategy is what does
not survive contact with out-of-sample data.

## Transaction costs (`--cost-bps`)

`--cost-bps` is a **one-way** cost in basis points (`5` = 5 bps = 0.0005), charged on
entry and on exit. It is applied to P&L only — never to the stop level or to the
entry/exit signal — so the trade *sequence* is byte-identical at every cost level and
`--cost-bps 0` reproduces the original exactly (`verify.py` still reports 8/8
trade-for-trade). That makes cost a clean isolated variable rather than something
that quietly reshapes the strategy.

Note `backtest_daily_walkforward.py` charges the entry side **twice** — once when
building `entry_price` (l.184: `best[1] * (1 + COST_BPS)`) and again inside `net_pnl`
(l.168: `entry_price * (1 + COST_BPS)`). This charges each side once.

### One config (buy=1.0, 852 trades, 20-day cache)

| one-way cost | return | win rate |
|---|---|---|
| 0 bps | -13.78% | 45.0% |
| 1 bps | -27.29% | 39.3% |
| 2 bps | -38.68% | 34.6% |
| 5 bps | -63.22% | 21.1% |
| 10 bps | -84.31% | 11.2% |

The average winning trade is **+0.163%**. A 5 bps round trip is 0.10%. The edge per
trade is barely larger than the friction, which is why win rate collapses from 45% to
21% — most "winners" were smaller than the spread.

### Walk-forward, 28 folds

| cost | mean TRAIN | mean TEST | median TEST | TEST>0 | test trades |
|---|---|---|---|---|---|
| 0 bps | +12.33% | -0.91% | -1.03% | 6/28 | 143 |
| 1 bps | +12.01% | -1.05% | -1.36% | 5/28 | 140 |
| 5 bps |  +9.94% | -1.44% | -1.67% | 4/28 | 140 |

Costs hurt walk-forward far less than they hurt the single config, and the reason is
informative: with costs on, the selector shifts toward **low-frequency** configs
(~5 test trades per fold, against 852 for buy=1.0), dodging the drag by trading less.
It is a real adaptation — and it still loses out of sample.

## Driver mode: `wf.py --walk`

`wf.py` without `--walk` runs ONE fold and prints JSON — that is what the farm
dispatches. With `--walk` it becomes the driver: it generates the folds, works out
what a fold costs, asks the farm whether distributing is worth it, ships itself to
the nodes if so, and summarises.

    python3 wf.py --walk --cache cache90.npz --cost-bps 5

    == 28 folds, 1200 configs each, ~14.4s per fold (11904 bars in cache)
    == farm: 3/3 nodes, 18 slots -> FLEET: fleet ~37.3s vs local ~67.2s over 18 slots
       sync anilraj@100.111.23.125: ok
       sync anilraj@100.112.6.92: ok
    == 28/28 folds produced a config in 30.5s
    == cost 5.0 bps | mean TRAIN +9.943% | mean TEST -1.442% | TEST>0 4/28

and with a grid too small to be worth spreading, unprompted:

    == 3 folds, 8 configs each, ~0.0s per fold (2688 bars in cache)
    == farm: 3/3 nodes, 18 slots -> LOCAL: task ~0.02s is under the ~0.5s SSH overhead

**Calibration, not a constant.** Per-fold cost depends on grid size, window length and
machine, so the driver times ~40 configs and scales rather than trusting a baked-in
number that goes stale the moment `--grid` changes. It currently runs ~2x high (14.4s
estimated against ~7.2s actual on the MSI), which biases toward distributing; the
FLEET/LOCAL calls have been right either way, but treat the seconds as an upper bound.

**`--sync`** (default on when distributing) scps `run.py`, `wf.py` and the cache to
every live node first, so a node that has never seen this workload still works. Turn
it off with `--no-sync` when nothing has changed.

**`--force local|fleet`** overrides the advice. Note `fleet` means *include remote
nodes*, not *avoid local*: with 3 tasks and 18 slots the local slots still take all
three, because they are created first.

Falling back to LOCAL still goes through `farm.py` (via `farmlib.Farm.local_only()`),
so the local path keeps the same slot parallelism, retries and per-task log capture as
the distributed one — one code path, two sizes of machine.

## Nightly validation (`nightly.py`)

A strategy validated once is not validated forever. `nightly.py` refetches bars,
re-runs the walk-forward across the farm, appends the result to
`history/nightly.jsonl`, and pushes to ntfy **only** when the out-of-sample number
moves materially or the run fails. A daily "still losing 1.4%" notification would
be noise, so the quiet path stays quiet.

    30 2 * * 1-5   cd .../sweep && python3 nightly.py --days 90 --cost-bps 5

Installed in the MSI's crontab, weeknights 02:30 local. ~90 s end to end: fetch
(~60 s) + 28 folds on the fleet (~25 s).

**Read-only by design.** It fetches market data and writes its own history file.
It never writes to live config, never places an order, and the live trader has no
knowledge of it. Drift detection, not optimisation — feeding a nightly best-fit back
into live parameters is how you automate overfitting.

`--drift-pp` (default 2.0) is the alert threshold against the trailing *median* of
previous runs, not the last one, so a single noisy night does not move the baseline.
Alerts carry direction and magnitude:

    [WF] out-of-sample degraded: -1.44% vs baseline +4.50% (-5.94pp) over 28 folds, 5bps

It runs on the MSI, not the droplet — the droplet is not on the tailnet and could not
reach the farm, and the live trader has no CPU-bound work to distribute anyway
(per-cycle it is Alpaca API calls plus LLM calls; `screen.py` is 49 lines of dict
filtering).

History and the nightly cache are gitignored: they are machine-local state.
