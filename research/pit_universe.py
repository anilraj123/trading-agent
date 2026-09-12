"""Point-in-time index membership from Wikipedia revision history.

WHY: research/universe.json is TODAY's membership, so backtesting it silently
excludes every company the index dropped -- bankruptcies, takeunders, chronic
losers. That inflates every result and inflates momentum most of all, because
buying past winners inside a universe selected FOR having won is close to
look-ahead.

Rather than reconstruct membership by replaying an "index changes" table
backwards (error-prone, and the S&P 500 page no longer carries one), this
fetches the ACTUAL page revision as it stood on each historical date and parses
the constituent table from it. No inference chain: what the table said on
2018-06-30 is what the index was on 2018-06-30, to Wikipedia's accuracy.

Output: research/_cache/pit_membership.json
        {"sp500": {"2016-01-31": [...symbols...], ...}, "sp400": {...}}
"""
import io, json, re, sys, time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "research" / "_cache"
OUT = CACHE / "pit_membership.json"
UA = "Mozilla/5.0 (X11; Linux x86_64) trading-agent research"
API = "https://en.wikipedia.org/w/api.php"
PAGES = {"sp500": "List of S&P 500 companies", "sp400": "List of S&P 400 companies"}

SYM_COLS = ("symbol", "ticker symbol", "ticker")


def month_ends(start=(2016, 1), end=(2026, 9)):
    out, y, m = [], *start
    while (y, m) <= end:
        nxt = (y + (m == 12), 1 if m == 12 else m + 1)
        out.append(date(*nxt, 1) - pd.Timedelta(days=1).to_pytimedelta())
        y, m = nxt
    return out


def _get(session, params, tries=6):
    """Wikipedia 429s readily. Back off rather than hammering it."""
    delay = 2.0
    for attempt in range(tries):
        r = session.get(API, params=params, timeout=60)
        if r.status_code == 429:
            time.sleep(delay); delay *= 2; continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("giving up after repeated 429s")


def revision_id(session, title, when):
    """Newest revision at or before `when`."""
    js = _get(session, {
        "action": "query", "prop": "revisions", "titles": title,
        "rvlimit": 1, "rvdir": "older", "rvstart": f"{when}T23:59:59Z",
        "rvprop": "ids|timestamp", "format": "json"})
    pages = js["query"]["pages"]
    for p in pages.values():
        revs = p.get("revisions") or []
        if revs:
            return revs[0]["revid"], revs[0]["timestamp"]
    return None, None


def symbols_from_revision(session, revid):
    html = _get(session, {"action": "parse", "oldid": revid,
                          "prop": "text", "format": "json"})["parse"]["text"]["*"]
    try:
        tables = pd.read_html(io.StringIO(html))
    except ValueError:
        return []
    best = []
    for t in tables:
        cols = [str(c).split("'")[-2].lower() if "(" in str(c) else str(c).lower()
                for c in t.columns]
        hit = next((i for i, c in enumerate(cols) if c.strip() in SYM_COLS), None)
        if hit is None or len(t) < 50:            # constituent tables are long
            continue
        syms = [re.sub(r"[^A-Z.\-]", "", str(s).upper()).replace(".", "-")
                for s in t.iloc[:, hit].tolist()]
        syms = [s for s in syms if 1 <= len(s) <= 6]
        if len(syms) > len(best):
            best = syms
    return sorted(set(best))


def build():
    CACHE.mkdir(parents=True, exist_ok=True)
    out = json.loads(OUT.read_text()) if OUT.exists() else {}
    s = requests.Session(); s.headers["User-Agent"] = UA
    for key, title in PAGES.items():
        out.setdefault(key, {})
        for d in month_ends():
            ds = d.isoformat()
            if ds in out[key]:
                continue
            rid, ts = revision_id(s, title, ds)
            if rid is None:
                print(f"  {key} {ds}: no revision"); continue
            syms = symbols_from_revision(s, rid)
            if len(syms) < 50:
                print(f"  {key} {ds}: parsed only {len(syms)} — skipping"); continue
            out[key][ds] = syms
            print(f"  {key} {ds}: {len(syms):4d} symbols  (rev {rid} @ {ts[:10]})", flush=True)
            OUT.write_text(json.dumps(out))
            time.sleep(1.5)                      # be polite to Wikipedia
    print(f"\nwrote {OUT}")
    for k, v in out.items():
        print(f"  {k}: {len(v)} monthly snapshots")


if __name__ == "__main__":
    build()
