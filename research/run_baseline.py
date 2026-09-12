"""Baseline: the CURRENTLY DEPLOYED configuration, over 2016-2026."""
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from research import data, engine, metrics

frames = data.load()
ind = engine.indicators(frames)
_, sectors = data.universe_symbols()
spy = frames["close"]["SPY"]

p = engine.Params()
trades, curve = engine.run(frames, ind, p, sectors=sectors)
m = metrics.summarize(curve, trades, bench=spy)

print("=" * 78)
print("BASELINE — live config: vol>=1.4x, |5d|<=7%, RSI 50-67, above both SMAs,")
print("           4 positions, 1/sector, -8% stop, +3%/-3% trail floored at entry,")
print("           5-day TTL, -5% invalidation, 10bps/side")
print("=" * 78)
for k, v in m.items():
    print(f"  {k:22} {v}")
print("\nEXITS:")
print(metrics.exit_breakdown(trades).to_string())
print(f"\nSPY buy-and-hold same window: {m.get('bench_return_pct')}%")
Path(ROOT / "research" / "_cache" / "baseline.json").write_text(json.dumps(m, indent=1))
trades.to_csv(ROOT / "research" / "_cache" / "baseline_trades.csv", index=False)
