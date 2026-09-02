"""Broad, fixed trading universe with GICS sectors.

Why this exists: the analyst used to see the top-12 of a scraped "trending"
list (Yahoo/Finviz/MarketWatch movers). Names in the news have, by
definition, already moved — which is why nearly every candidate failed the
extended-move gate and the book kept chasing (NBTX, BHVN, LEU). The nightly
screen now runs over the S&P 500 + S&P 400 (liquid large + mid caps), and
the analyst only sees names that already pass the winning-profile screen.

Source: the Wikipedia constituent tables (symbol + GICS sector), cached under
DATA_DIR/universe_cache.json and refreshed every UNIVERSE_REFRESH_DAYS. If
the fetch fails the cache is used regardless of age; if there is no cache,
a small hardcoded fallback (no sectors) keeps research alive.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

from .config import V2Config

logger = logging.getLogger("trader_v2.universe")

WIKI_PAGES = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "sp400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
}
CACHE_FILE = f"{V2Config.DATA_DIR}/universe_cache.json"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) trading-agent research"

# Last-resort universe (no sectors): the shared discovery module's core list
# minus ETFs. Only used when both the fetch and the cache are unavailable.
FALLBACK = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "JPM", "V", "JNJ",
    "WMT", "PG", "MA", "UNH", "HD", "DIS", "BAC", "XOM", "PFE", "CSCO",
    "INTC", "VZ", "KO", "PEP", "MRK", "ABT", "TMO", "COST", "NFLX", "ADBE",
    "CRM", "AMD", "QCOM", "TXN", "AVGO", "ORCL", "ACN", "LLY", "DHR", "NKE",
    "NEE", "BMY", "UNP", "LOW", "PM", "RTX", "LIN", "HON", "AMGN", "SPGI",
    "BLK", "SBUX", "CAT", "GS", "AXP", "DE", "IBM", "GE", "ISRG", "NOW",
    "INTU", "TJX", "AMT", "CVS", "PLD", "MDT", "ZTS", "SYK", "ADP", "BKNG",
    "MMM", "CI", "MO", "GILD", "REGN", "VRTX", "MU", "LRCX", "ADI", "KLAC",
    "AMAT", "MCHP", "SNPS", "CDNS", "MRVL", "NXPI", "ON",
]


def normalize_symbol(sym: str) -> str:
    """Wikipedia writes class shares as BRK.B / BF.B; Alpaca uses the same
    dotted form, so only whitespace/case is normalised."""
    return str(sym or "").strip().upper()


def parse_constituents(html: str) -> dict:
    """{symbol: gics_sector} from one Wikipedia constituents page. Pure —
    tested against a snippet. Finds the 'Symbol' and 'GICS Sector' columns by
    header text (Wikipedia renders it as 'GICSSector' after stripping), so a
    column reorder does not silently break the universe."""
    soup = BeautifulSoup(html or "", "html.parser")
    table = soup.find("table", id="constituents") or soup.find("table", class_="wikitable")
    if table is None:
        return {}
    rows = table.find_all("tr")
    if not rows:
        return {}
    headers = [c.get_text(strip=True).lower().replace(" ", "") for c in rows[0].find_all(["th", "td"])]
    try:
        i_sym = headers.index("symbol")
    except ValueError:
        return {}
    i_sec = next((i for i, h in enumerate(headers) if h.startswith("gicssector")), None)
    out = {}
    for r in rows[1:]:
        cells = [c.get_text(strip=True) for c in r.find_all(["td", "th"])]
        if len(cells) <= i_sym:
            continue
        sym = normalize_symbol(cells[i_sym])
        if not sym or " " in sym or len(sym) > 6:
            continue
        sector = cells[i_sec] if i_sec is not None and len(cells) > i_sec and cells[i_sec] else None
        out[sym] = sector
    return out


def cache_is_fresh(fetched_iso: str, now: datetime, refresh_days: int) -> bool:
    try:
        fetched = datetime.fromisoformat(str(fetched_iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return now - fetched < timedelta(days=refresh_days)


def _read_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_cache(members: dict, sources: list):
    os.makedirs(V2Config.DATA_DIR, exist_ok=True)
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"fetched": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "sources": sources, "members": members}, f)
    os.replace(tmp, CACHE_FILE)


def fetch_members(sources: list, session=None, timeout: int = 20) -> dict:
    """{symbol: sector} across the requested index pages. Raises if NO page
    could be parsed (a partial result is still a universe)."""
    session = session or requests.Session()
    # Explicit, not setdefault: a Session already carries python-requests'
    # own User-Agent, which Wikipedia answers with 403.
    session.headers["User-Agent"] = USER_AGENT
    session.headers["Accept"] = "text/html"
    members, ok = {}, []
    for src in sources:
        url = WIKI_PAGES.get(src)
        if not url:
            logger.warning(f"unknown universe source {src!r}")
            continue
        try:
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
            got = parse_constituents(resp.text)
            if not got:
                raise ValueError("no rows parsed")
            members.update(got)
            ok.append(src)
            logger.info(f"universe {src}: {len(got)} members")
        except Exception as e:
            logger.warning(f"universe fetch failed for {src}: {e}")
    if not ok:
        raise RuntimeError("no universe source could be fetched")
    return members


def load_universe(session=None) -> tuple:
    """({symbol: sector}, source_label). source_label is one of
    'fetched' | 'cache' | 'cache-stale' | 'fallback' and is journalled."""
    sources = [s.strip() for s in V2Config.UNIVERSE_SOURCES.split(",") if s.strip()]
    cache = _read_cache()
    now = datetime.now(timezone.utc)
    if cache and cache_is_fresh(cache.get("fetched"), now, V2Config.UNIVERSE_REFRESH_DAYS) \
            and cache.get("members"):
        return dict(cache["members"]), "cache"
    try:
        members = fetch_members(sources, session=session)
        _write_cache(members, sources)
        return members, "fetched"
    except Exception as e:
        logger.warning(f"universe refresh failed ({e}); using cache/fallback")
    if cache and cache.get("members"):
        return dict(cache["members"]), "cache-stale"
    return {s: None for s in FALLBACK}, "fallback"
