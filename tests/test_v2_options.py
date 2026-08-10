"""Unit tests for trader_v2/options.py pure logic.

Same conventions as test_v2_logic.py: env pinned before imports; everything
here is pure (no network, no Alpaca, no LLM).
"""
import sys
import os
from datetime import date

test_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, test_root)

os.environ["DATA_DIR"] = "/tmp/test_data_v2"
os.environ["ALPACA_BASE_URL"] = "https://paper-api.alpaca.markets"

import pytest

from trader_v2 import options as opt
from trader_v2.config import V2Config

TODAY = date(2026, 8, 10)
TIERS = opt.parse_stop_tiers("5:-0.25,14:-0.40,-0.55")


def row(**over):
    base = dict(symbol="NVDA260918C00185000", type="call", strike=185.0,
                expiry=date(2026, 9, 18), dte=39, bid=2.30, ask=2.50,
                mid=2.40, delta=0.52, iv=0.45, oi=500)
    base.update(over)
    return base


def entered_option_thesis(**over):
    t = dict(status="entered", entry_price=2.40, invalidation_price=170.0,
             expires="2026-08-18", pending_close=False,
             option={"contract": "NVDA260918C00185000", "type": "call",
                     "strike": 185.0, "expiry": "2026-09-18"})
    t.update(over)
    return t


SELECT_KW = dict(delta_min=0.40, delta_max=0.60, dte_min=30, dte_max=45,
                 min_oi=100, max_spread_pct=0.10, min_premium=50, max_premium=280)


class TestParseOsi:
    def test_call_round_trip(self):
        p = opt.parse_osi("AAPL260918C00190000")
        assert p == {"underlying": "AAPL", "expiry": date(2026, 9, 18),
                     "type": "call", "strike": 190.0}

    def test_put_fractional_strike(self):
        p = opt.parse_osi("F260918P00012500")
        assert p["type"] == "put" and p["strike"] == 12.5 and p["underlying"] == "F"

    def test_equity_symbol_rejected(self):
        assert opt.parse_osi("AAPL") is None

    def test_malformed_rejected(self):
        assert opt.parse_osi("AAPL26131XC00190000") is None      # bad month
        assert opt.parse_osi("AAPL260918X00190000") is None      # bad type char
        assert opt.parse_osi("1234260918C00190000") is None      # numeric root
        assert opt.parse_osi("") is None
        assert opt.parse_osi(None) is None


class TestStopTiers:
    def test_parse(self):
        assert TIERS == ((5, -0.25), (14, -0.40), (None, -0.55))

    def test_boundaries(self):
        assert opt.premium_stop_pct(5, TIERS) == -0.25
        assert opt.premium_stop_pct(6, TIERS) == -0.40
        assert opt.premium_stop_pct(14, TIERS) == -0.40
        assert opt.premium_stop_pct(15, TIERS) == -0.55
        assert opt.premium_stop_pct(45, TIERS) == -0.55

    def test_bad_specs_rejected(self):
        with pytest.raises(ValueError):
            opt.parse_stop_tiers("5:-0.25,14:-0.40")     # no catch-all
        with pytest.raises(ValueError):
            opt.parse_stop_tiers("14:-0.40,5:-0.25,-0.55")  # out of order

    def test_config_default_spec_parses(self):
        assert opt.parse_stop_tiers(V2Config.OPT_STOP_TIERS_SPEC)


class TestQuoteMath:
    def test_spread_pct(self):
        assert opt.spread_pct(2.30, 2.50) == pytest.approx(0.2 / 2.4)

    def test_spread_pct_unusable(self):
        assert opt.spread_pct(0, 2.50) is None       # no bid
        assert opt.spread_pct(2.50, 0) is None       # no ask
        assert opt.spread_pct(None, 2.50) is None
        assert opt.spread_pct(2.60, 2.50) is None    # crossed

    def test_tick_round_below_3(self):
        assert opt.tick_round(2.437, up=False) == 2.43
        assert opt.tick_round(2.431, up=True) == 2.44

    def test_tick_round_at_or_above_3(self):
        assert opt.tick_round(3.42, up=False) == 3.40
        assert opt.tick_round(3.41, up=True) == 3.45

    def test_tick_round_exact_is_stable(self):
        assert opt.tick_round(2.43, up=True) == 2.43
        assert opt.tick_round(3.45, up=False) == 3.45


class TestSelectContract:
    def test_happy_path_picks_nearest_half_delta(self):
        rows = [row(symbol="A", delta=0.58), row(symbol="B", delta=0.51),
                row(symbol="C", delta=0.43)]
        picked, why = opt.select_contract(rows, "call", **SELECT_KW)
        assert why is None and picked["symbol"] == "B"

    def test_put_uses_delta_magnitude(self):
        rows = [row(symbol="P1", type="put", delta=-0.48)]
        picked, why = opt.select_contract(rows, "put", **SELECT_KW)
        assert why is None and picked["symbol"] == "P1"

    def test_wrong_side_excluded_silently(self):
        picked, why = opt.select_contract([row(type="put", delta=-0.5)], "call", **SELECT_KW)
        assert picked is None and why == "empty_chain"

    def test_missing_greeks_rejected(self):
        _, why = opt.select_contract([row(delta=None)], "call", **SELECT_KW)
        assert "no_greeks:1" in why

    def test_delta_out_of_range(self):
        _, why = opt.select_contract([row(delta=0.25), row(delta=0.75)], "call", **SELECT_KW)
        assert "delta_range:2" in why

    def test_dte_out_of_range(self):
        _, why = opt.select_contract([row(dte=20), row(dte=60)], "call", **SELECT_KW)
        assert "dte_range:2" in why

    def test_low_oi(self):
        _, why = opt.select_contract([row(oi=99)], "call", **SELECT_KW)
        assert "low_oi:1" in why

    def test_wide_spread(self):
        _, why = opt.select_contract([row(bid=2.00, ask=2.80, mid=2.40)], "call", **SELECT_KW)
        assert "wide_spread:1" in why

    def test_bad_quote(self):
        _, why = opt.select_contract([row(bid=None, ask=None, mid=None)], "call", **SELECT_KW)
        assert "bad_quote:1" in why

    def test_over_budget(self):
        _, why = opt.select_contract([row(bid=3.30, ask=3.50, mid=3.40)], "call", **SELECT_KW)
        assert "over_budget:1" in why  # 340 > 280

    def test_under_min_premium(self):
        _, why = opt.select_contract([row(bid=0.40, ask=0.44, mid=0.42)], "call", **SELECT_KW)
        assert "under_min_premium:1" in why  # $42 < $50

    def test_spread_tiebreak(self):
        rows = [row(symbol="WIDE", delta=0.50, bid=2.30, ask=2.50, mid=2.40),
                row(symbol="TIGHT", delta=0.50, bid=2.36, ask=2.44, mid=2.40)]
        picked, _ = opt.select_contract(rows, "call", **SELECT_KW)
        assert picked["symbol"] == "TIGHT"

    def test_dte_tiebreak_prefers_longer(self):
        rows = [row(symbol="NEAR", dte=32), row(symbol="FAR", dte=44)]
        picked, _ = opt.select_contract(rows, "call", **SELECT_KW)
        assert picked["symbol"] == "FAR"

    def test_empty_chain(self):
        picked, why = opt.select_contract([], "call", **SELECT_KW)
        assert picked is None and why == "empty_chain"


class TestContractsForBudget:
    def test_integer_floor(self):
        assert opt.contracts_for_budget(2.40, budget=270, cash=1000) == 1  # 240 fits, 480 doesn't

    def test_multiple(self):
        assert opt.contracts_for_budget(1.00, budget=270, cash=1000) == 2

    def test_cash_clamps(self):
        assert opt.contracts_for_budget(1.00, budget=270, cash=150) == 1

    def test_zero_when_unaffordable(self):
        assert opt.contracts_for_budget(3.00, budget=270, cash=1000) == 0

    def test_bad_premium(self):
        assert opt.contracts_for_budget(None, 270, 1000) == 0
        assert opt.contracts_for_budget(0, 270, 1000) == 0


class TestOptionExitDecision:
    def _decide(self, t, premium=2.40, under=180.0, dte_val=39,
                close_window=False, today=TODAY):
        return opt.option_exit_decision(
            t, premium, under, dte_val, close_window, today,
            force_exit_dte=3, stop_tiers=TIERS, take_profit_pct=0.60)

    def test_hold(self):
        assert self._decide(entered_option_thesis()) is None

    def test_not_entered(self):
        assert self._decide(entered_option_thesis(status="active")) is None

    def test_expiry_force_exit_dte1_any_cycle(self):
        assert self._decide(entered_option_thesis(), dte_val=1) == "expiry_force_exit"

    def test_expiry_force_exit_dte3_close_window_only(self):
        t = entered_option_thesis()
        assert self._decide(t, dte_val=3) is None
        assert self._decide(t, dte_val=3, close_window=True) == "expiry_force_exit"

    def test_expiry_force_exit_unknown_dte(self):
        assert self._decide(entered_option_thesis(), dte_val=None) == "expiry_force_exit"

    def test_premium_stop_tiered(self):
        t = entered_option_thesis(entry_price=2.40)
        # -50% at 39 DTE: tier is -55, holds
        assert self._decide(t, premium=1.20, dte_val=39) is None
        # -50% at 10 DTE: tier is -40, stops
        assert self._decide(t, premium=1.20, dte_val=10) == "premium_stop"
        # -30% at 5 DTE: tier is -25, stops
        assert self._decide(t, premium=1.68, dte_val=5) == "premium_stop"

    def test_premium_stop_beats_research_close(self):
        t = entered_option_thesis(pending_close=True)
        assert self._decide(t, premium=0.90, dte_val=10) == "premium_stop"

    def test_research_close(self):
        assert self._decide(entered_option_thesis(pending_close=True)) == "research_close"

    def test_invalidation_call_close_window_only(self):
        t = entered_option_thesis()
        assert self._decide(t, under=169.0) is None
        assert self._decide(t, under=169.0, close_window=True) == "invalidation"

    def test_invalidation_put_inverts(self):
        t = entered_option_thesis(invalidation_price=190.0)
        t["option"]["type"] = "put"
        assert self._decide(t, under=191.0, close_window=True) == "invalidation"
        assert self._decide(t, under=185.0, close_window=True) is None

    def test_take_profit_any_cycle(self):
        t = entered_option_thesis(entry_price=2.40)
        assert self._decide(t, premium=3.84) == "take_profit"   # exactly +60%
        assert self._decide(t, premium=3.80) is None

    def test_ttl_expiry_close_window(self):
        t = entered_option_thesis(expires="2026-08-10")
        assert self._decide(t) is None
        assert self._decide(t, close_window=True) == "ttl_expiry"

    def test_no_premium_still_force_exits(self):
        assert self._decide(entered_option_thesis(), premium=None, dte_val=1) == "expiry_force_exit"

    def test_no_premium_no_stop(self):
        assert self._decide(entered_option_thesis(), premium=None) is None


class TestConfigInvariants:
    def test_validate_passes_with_defaults(self, monkeypatch):
        # Bypass the paper-endpoint guard: it reads whatever env the test host
        # has (the dev machine's .env is the live URL). The invariant chain —
        # delta/DTE/premium bounds + stop-tier spec parse — is what's under test.
        monkeypatch.setattr(V2Config, "REQUIRE_PAPER", False)
        V2Config.validate()
