"""Snapshot the S&P 500+400 universe to research/universe.json (one-off)."""
import json, os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
os.environ.setdefault("DATA_DIR", str(ROOT / "research" / "_cache"))
os.makedirs(os.environ["DATA_DIR"], exist_ok=True)
from trader_v2 import universe
members, source = universe.load_universe()
out = ROOT / "research" / "universe.json"
out.write_text(json.dumps({"source": source, "members": members}, indent=1, sort_keys=True))
sectors = {}
for s, sec in members.items():
    sectors[sec] = sectors.get(sec, 0) + 1
print(f"source={source}  symbols={len(members)}")
for k, v in sorted(sectors.items(), key=lambda x: -x[1]):
    print(f"  {v:4d}  {k}")
