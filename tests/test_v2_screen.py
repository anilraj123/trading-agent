"""Unit tests for the nightly universe screen + sector cap (pure logic:
trader_v2/universe.py parsing/caching, trader_v2/screen.py, and
thesis.select_with_sector_cap). No network."""
import sys
import os
from datetime import date, datetime, timedelta, timezone

test_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, test_root)

os.environ["DATA_DIR"] = "/tmp/test_data_v2"
os.environ["ALPACA_BASE_URL"] = "https://paper-api.alpaca.markets"

from trader_v2 import screen, universe
from trader_v2 import thesis as th
from trader_v2.config import V2Config

TODAY = date(2026, 9, 2)
GATES = dict(min_volume_ratio=1.4, max_move_5d_pct=7.0, max_rsi=67, min_rsi=50)


def row(**over):
    base = dict(symbol="X", close=100.0, rsi_14=60.0, sma_20=95.0, sma_50=90.0,
                volume_ratio=1.8, chg_5d_pct=3.0, sector="Industrials")
    base.update(over)
    return base


WIKI = """
<table id="constituents" class="wikitable"><tbody>
<tr><th>Symbol</th><th>Security</th><th>GICS Sector</th><th>GICS Sub-Industry</th></tr>
<tr><td><a href="#">MMM</a></td><td>3M</td><td>Industrials</td><td>Conglomerates</td></tr>
<tr><td>BRK.B</td><td>Berkshire</td><td>Financials</td><td>Multi-Sector Holdings</td></tr>
<tr><td> nvda </td><td>Nvidia</td><td>Information Technology</td><td>Semis</td></tr>
<tr><td>BAD SYM</td><td>junk</td><td>Energy</td><td>x</td></tr>
</tbody></table>"""


class TestUniverseParse:
    def test_parses_symbol_and_sector(self):
        m = universe.parse_constituents(WIKI)
        assert m == {"MMM": "Industrials", "BRK.B": "Financials",
                     "NVDA": "Information Technology"}

    def test_column_order_found_by_header(self):
        html = WIKI.replace("<th>Symbol</th><th>Security</th><th>GICS Sector</th>",
                            "<th>GICS Sector</th><th>Security</th><th>Symbol</th>")
        html = html.replace("<td><a href=\"#\">MMM</a></td><td>3M</td><td>Industrials</td>",
                            "<td>Industrials</td><td>3M</td><td><a href=\"#\">MMM</a></td>")
        assert universe.parse_constituents(html)["MMM"] == "Industrials"

    def test_no_table_or_no_symbol_column_is_empty(self):
        assert universe.parse_constituents("<html></html>") == {}
        assert universe.parse_constituents(WIKI.replace("Symbol", "Ticker")) == {}
        assert universe.parse_constituents("") == {}

    def test_cache_freshness(self):
        now = datetime(2026, 9, 2, tzinfo=timezone.utc)
        assert universe.cache_is_fresh("2026-08-30T00:00:00Z", now, 7)
        assert not universe.cache_is_fresh("2026-08-20T00:00:00Z", now, 7)
        assert not universe.cache_is_fresh(None, now, 7)
        assert not universe.cache_is_fresh("garbage", now, 7)

    def test_fallback_has_no_etfs(self):
        assert "SPY" not in universe.FALLBACK and "QQQ" not in universe.FALLBACK
        assert len(universe.FALLBACK) > 50


class TestProfile:
    def test_winning_profile_is_bullish(self):
        assert screen.profile_of(row(), **GATES) == "bullish"

    def test_each_bullish_condition(self):
        assert screen.profile_of(row(volume_ratio=1.2), **GATES) is None      # hard gate
        assert screen.profile_of(row(chg_5d_pct=9.0), **GATES) is None        # extended
        assert screen.profile_of(row(rsi_14=68.0), **GATES) is None           # RSI cap
        assert screen.profile_of(row(rsi_14=45.0), **GATES) is None           # RSI floor
        assert screen.profile_of(row(close=92.0), **GATES) is None            # below SMA20
        assert screen.profile_of(row(sma_50=101.0), **GATES) is None          # below SMA50

    def test_bearish_mirror(self):
        r = row(close=80.0, sma_20=85.0, sma_50=90.0, rsi_14=40.0, chg_5d_pct=-3.0)
        assert screen.profile_of(r, **GATES) == "bearish"
        assert screen.profile_of(dict(r, rsi_14=30.0), **GATES) is None      # below 100-67
        assert screen.profile_of(dict(r, rsi_14=52.0), **GATES) is None      # above 100-50
        assert screen.profile_of(dict(r, chg_5d_pct=-9.0), **GATES) is None  # extended drop

    def test_missing_data_never_qualifies(self):
        assert screen.profile_of(row(rsi_14=None), **GATES) is None
        assert screen.profile_of(row(sma_50=None), **GATES) is None
        assert screen.profile_of(row(close=None), **GATES) is None

    def test_trend_requirement_can_be_switched_off(self):
        assert screen.profile_of(row(sma_50=None), require_trend=False, **GATES) == "bullish"
        assert screen.profile_of(row(close=92.0), require_trend=False, **GATES) == "bullish"

    def test_real_week_examples(self):
        # NVDA 8/27 (the one clean entry): vol 2.34x, +6.18%, RSI 53.5, above both SMAs
        assert screen.profile_of(row(close=227.98, sma_20=217.21, sma_50=208.16,
                                     rsi_14=53.5, volume_ratio=2.34, chg_5d_pct=6.18), **GATES) == "bullish"
        # NOW 9/1: +13.59% and 0.91x -> never reaches the analyst
        assert screen.profile_of(row(close=142.9, sma_20=128.41, sma_50=114.13,
                                     rsi_14=66.29, volume_ratio=0.91, chg_5d_pct=13.59), **GATES) is None


class TestRank:
    def test_ranks_by_volume_within_profile_and_truncates(self):
        rows = [dict(row(symbol="A", volume_ratio=1.5), profile="bullish"),
                dict(row(symbol="B", volume_ratio=2.5), profile="bullish"),
                dict(row(symbol="C", volume_ratio=1.9), profile="bullish"),
                dict(row(symbol="D", volume_ratio=3.0), profile="bearish"),
                dict(row(symbol="E", volume_ratio=9.0), profile=None),
                dict(row(symbol="F", volume_ratio=9.0), profile="book")]
        out = screen.rank(rows, n_bullish=2, n_bearish=1)
        assert [r["symbol"] for r in out] == ["B", "C", "D"]
        assert screen.rank(rows, 2, 0) and all(r["profile"] == "bullish" for r in screen.rank(rows, 2, 0))
        assert screen.rank(rows, 0, 0) == []


def raw(sym, conviction=4):
    return dict(symbol=sym, conviction=conviction, entry_zone_low=98.0, entry_zone_high=102.0,
                invalidation_price=97.5, price_target=115.0, catalyst="c", ttl_days=5, reasoning="r")


class TestSectorCap:
    def _t(self, sym, sector, conviction=4):
        return th.build_thesis(raw(sym, conviction), TODAY, sector=sector)

    def test_second_thesis_in_same_sector_dropped_lower_conviction_loses(self):
        v = [self._t("SMR", "Utilities", 3), self._t("LEU", "Utilities", 4), self._t("NVDA", "Information Technology", 4)]
        sel, dropped = th.select_with_sector_cap(v, capacity=5, max_per_sector=1)
        assert [t["symbol"] for t in sel] == ["LEU", "NVDA"]
        assert dropped[0][0]["symbol"] == "SMR" and "sector cap: Utilities" in dropped[0][1]

    def test_held_sector_blocks_new_thesis(self):
        v = [self._t("BHVN", "Health Care", 4)]
        sel, dropped = th.select_with_sector_cap(v, 5, 1, taken_sectors=["Health Care", None])
        assert sel == [] and "already has 1" in dropped[0][1]

    def test_unknown_sector_never_capped(self):
        v = [self._t("A", None), self._t("B", None), self._t("C", None)]
        sel, dropped = th.select_with_sector_cap(v, 5, 1)
        assert len(sel) == 3 and dropped == []

    def test_capacity_still_truncates_silently(self):
        v = [self._t("A", "Energy"), self._t("B", "Materials"), self._t("C", "Financials")]
        sel, dropped = th.select_with_sector_cap(v, 2, 1)
        assert len(sel) == 2 and dropped == []

    def test_zero_disables(self):
        v = [self._t("A", "Energy"), self._t("B", "Energy")]
        sel, dropped = th.select_with_sector_cap(v, 5, 0)
        assert len(sel) == 2 and dropped == []

    def test_sector_stored_on_thesis(self):
        assert self._t("A", "Energy")["sector"] == "Energy"
        assert th.build_thesis(raw("A"), TODAY)["sector"] is None

    def test_config_defaults(self):
        assert V2Config.MAX_PER_SECTOR == 1 and V2Config.SCREEN_MIN_RSI == 50
        assert V2Config.SCREEN_BAR_DAYS >= 90 and V2Config.UNIVERSE_SOURCES == "sp500,sp400"
