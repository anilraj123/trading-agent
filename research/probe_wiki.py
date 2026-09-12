import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pandas as pd, requests
UA = "Mozilla/5.0 (X11; Linux x86_64) trading-agent research"
for url in ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"):
    html = requests.get(url, headers={"User-Agent": UA}, timeout=30).text
    tables = pd.read_html(html)
    print(f"\n{url.rsplit('/',1)[-1]}: {len(tables)} tables")
    for i, t in enumerate(tables[:6]):
        cols = [str(c) for c in t.columns.tolist()][:7]
        print(f"  [{i}] {t.shape}  {cols}")
