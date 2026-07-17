"""Unit tests for trader_v2 pure logic (thesis.py, store lessons bounding).

Same conventions as test_pure_logic.py: env pinned before imports so config is
deterministic; everything tested here is pure (no network, no Alpaca, no LLM).
"""
import sys
import os
from datetime import date

test_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, test_root)

os.environ["DATA_DIR"] = "/tmp/test_data_v2"
os.environ["ALPACA_BASE_URL"] = "https://paper-api.alpaca.markets"
os.environ["V2_CAPITAL"] = "1350"
os.environ["V2_MAX_POSITIONS"] = "4"
os.environ["V2_MAX_THESES"] = "5"
os.environ["V2_POSITION_CAP_PCT"] = "0.30"
os.environ["V2_DISASTER_STOP_PCT"] = "-0.08"
os.environ["V2_TRAIL_ACTIVATE_PCT"] = "0.03"
os.environ["V2_TRAIL_STOP_PCT"] = "-0.03"
os.environ["V2_DAILY_LOSS_HALT_PCT"] = "-0.05"
os.environ["V2_LESSONS_MAX_CHARS"] = "200"   # small bound for testing

from trader_v2 import thesis as th
from trader_v2.config import V2Config

TODAY = date(2026, 7, 17)
CANDIDATES = {"NVDA", "AMD", "AVGO", "MRVL"}


def raw_thesis(**over):
    base = dict(symbol="NVDA", conviction=4, entry_zone_low=172.0,
                entry_zone_high=178.5, invalidation_price=165.0,
                price_target=195.0, catalyst="earnings 7/22", ttl_days=5,
                reasoning="test")
    base.update(over)
    return base


def make_entered(entry=100.0, qty=3.0, **over):
    t = th.build_thesis(raw_thesis(symbol="NVDA", entry_zone_low=entry * 0.98,
                                   entry_zone_high=entry * 1.02,
                                   invalidation_price=entry * 0.93,
                                   price_target=entry * 1.15), TODAY)
    th.apply_fill(t, entry, qty, "ord1")
    t.update(over)
    return t


class TestValidation:
    def _ok(self, raw, close=175.0, entered=frozenset()):
        return th.validate_new_thesis(raw, close, CANDIDATES, set(entered))

    def test_happy_path(self):
        assert self._ok(raw_thesis()) is None

    def test_hallucinated_symbol(self):
        assert "candidate set" in self._ok(raw_thesis(symbol="ZZZZ"))

    def test_blacklist_and_crypto(self):
        assert "blacklisted" in th.validate_new_thesis(
            raw_thesis(), 175.0, CANDIDATES, set(), blacklist={"NVDA"})
        assert "crypto" in th.validate_new_thesis(
            raw_thesis(symbol="NVDAUSD"), 175.0, CANDIDATES | {"NVDAUSD"}, set(),
            crypto_suffixes=("USD",))

    def test_already_entered_symbol(self):
        assert "entered" in self._ok(raw_thesis(), entered={"NVDA"})

    def test_conviction_bounds(self):
        assert self._ok(raw_thesis(conviction=0)) is not None
        assert self._ok(raw_thesis(conviction=6)) is not None

    def test_inverted_or_zero_zone(self):
        assert "bad entry zone" in self._ok(raw_thesis(entry_zone_low=180, entry_zone_high=172))
        assert "bad entry zone" in self._ok(raw_thesis(entry_zone_low=0, entry_zone_high=172))

    def test_zone_too_wide(self):
        assert "wider" in self._ok(raw_thesis(entry_zone_low=100, entry_zone_high=120))

    def test_zone_beyond_drift_of_close(self):
        # close 175; zone [210, 215] is >15% above
        assert "beyond" in self._ok(raw_thesis(entry_zone_low=210, entry_zone_high=215,
                                               invalidation_price=200, price_target=240))

    def test_invalidation_must_be_below_zone(self):
        assert "below zone low" in self._ok(raw_thesis(invalidation_price=173.0))

    def test_invalidation_too_deep(self):
        assert "deeper" in self._ok(raw_thesis(invalidation_price=100.0))

    def test_target_above_zone(self):
        assert "exceed zone high" in self._ok(raw_thesis(price_target=178.0))

    def test_ttl_bounds(self):
        assert "ttl" in self._ok(raw_thesis(ttl_days=0))
        assert "ttl" in self._ok(raw_thesis(ttl_days=11))

    def test_malformed(self):
        assert "malformed" in self._ok({"symbol": "NVDA"})

    def test_select_dedupes_and_truncates_by_conviction(self):
        ts = [th.build_thesis(raw_thesis(symbol=s, conviction=c), TODAY)
              for s, c in [("NVDA", 3), ("NVDA", 5), ("AMD", 4), ("AVGO", 2), ("MRVL", 3)]]
        sel = th.select_theses(ts, capacity=3)
        assert [t["symbol"] for t in sel] == ["NVDA", "AMD", "MRVL"]
        assert sel[0]["conviction"] == 5  # dedupe kept the higher-conviction NVDA


class TestEntryLogic:
    def test_zone_inclusive_bounds(self):
        assert th.in_entry_zone(172.0, [172.0, 178.5])
        assert th.in_entry_zone(178.5, [172.0, 178.5])
        assert not th.in_entry_zone(178.51, [172.0, 178.5])

    def test_skip_reasons(self):
        assert th.entry_skip_reason(180.0, [172, 178.5]) == "above_zone"
        assert th.entry_skip_reason(170.0, [172, 178.5]) == "below_zone"
        assert th.entry_skip_reason(175.0, [172, 178.5]) is None

    def test_position_size_equal_weight(self):
        qty = th.position_size(1350, 4, 0.30, cash=10000, price=100, min_notional=10)
        assert abs(qty * 100 - 337.5) < 1e-6

    def test_position_size_cap_binds(self):
        # 2 positions -> slice 675 > cap 405 (30% of 1350)
        qty = th.position_size(1350, 2, 0.30, cash=10000, price=100, min_notional=10)
        assert abs(qty * 100 - 405.0) < 1e-6

    def test_position_size_cash_clamp_and_min_notional(self):
        qty = th.position_size(1350, 4, 0.30, cash=100, price=100, min_notional=10)
        assert abs(qty * 100 - 98.0) < 1e-6
        assert th.position_size(1350, 4, 0.30, cash=5, price=100, min_notional=10) == 0.0

    def test_position_size_bad_price(self):
        assert th.position_size(1350, 4, 0.30, cash=1000, price=0, min_notional=10) == 0.0
        assert th.position_size(1350, 4, 0.30, cash=1000, price=None, min_notional=10) == 0.0


class TestExitPrecedence:
    def test_disaster_fires_any_time(self):
        t = make_entered(100.0)
        assert th.exit_decision(t, 92.0, False, -0.08, -0.03, TODAY) == "disaster_stop"

    def test_disaster_beats_everything(self):
        t = make_entered(100.0, pending_close=True, trailing=True, hwm=110.0)
        assert th.exit_decision(t, 91.0, True, -0.08, -0.03, TODAY) == "disaster_stop"

    def test_pending_close_beats_invalidation(self):
        t = make_entered(100.0, pending_close=True)
        assert th.exit_decision(t, 92.5, True, -0.08, -0.03, TODAY) == "research_close"

    def test_invalidation_close_window_only(self):
        t = make_entered(100.0)  # invalidation at 93
        assert th.exit_decision(t, 92.5, False, -0.08, -0.03, TODAY) is None
        assert th.exit_decision(t, 92.5, True, -0.08, -0.03, TODAY) == "invalidation"

    def test_trailing_fires_any_time_once_activated(self):
        t = make_entered(100.0, trailing=True, hwm=110.0)
        assert th.exit_decision(t, 106.6, False, -0.08, -0.03, TODAY) == "trailing_stop"
        assert th.exit_decision(t, 106.8, False, -0.08, -0.03, TODAY) is None

    def test_not_trailing_no_trail_exit(self):
        t = make_entered(100.0, hwm=102.0)
        assert th.exit_decision(t, 99.5, False, -0.08, -0.03, TODAY) is None

    def test_ttl_close_window_only(self):
        t = make_entered(100.0)
        t["expires"] = TODAY.isoformat()
        assert th.exit_decision(t, 101.0, False, -0.08, -0.03, TODAY) is None
        assert th.exit_decision(t, 101.0, True, -0.08, -0.03, TODAY) == "ttl_expiry"

    def test_boundary_equality_fires(self):
        t = make_entered(100.0)
        assert th.exit_decision(t, 92.0, False, -0.08, -0.03, TODAY) == "disaster_stop"
        t2 = make_entered(100.0, trailing=True, hwm=110.0)
        assert th.exit_decision(t2, 110.0 * 0.97, False, -0.08, -0.03, TODAY) == "trailing_stop"

    def test_no_exit_healthy(self):
        t = make_entered(100.0)
        assert th.exit_decision(t, 101.0, True, -0.08, -0.03, TODAY) is None


class TestTrail:
    def test_ratchets_up_only_and_sticky(self):
        t = make_entered(100.0)
        th.update_trail(t, 102.0, 0.03)
        assert t["hwm"] == 102.0 and t["trailing"] is False
        th.update_trail(t, 103.0, 0.03)
        assert t["trailing"] is True
        th.update_trail(t, 99.0, 0.03)
        assert t["hwm"] == 103.0 and t["trailing"] is True  # sticky, hwm never down

    def test_activation_exact_boundary(self):
        t = make_entered(100.0)
        th.update_trail(t, 103.0, 0.03)
        assert t["trailing"] is True

    def test_ignores_non_entered(self):
        t = th.build_thesis(raw_thesis(), TODAY)
        assert th.update_trail(t, 200.0, 0.03) is False


class TestTransitions:
    def test_fill_sets_state(self):
        t = th.build_thesis(raw_thesis(), TODAY)
        th.apply_fill(t, 175.0, 1.9, "oid")
        assert t["status"] == "entered" and t["hwm"] == 175.0 and t["qty"] == 1.9

    def test_double_entry_refused(self):
        t = make_entered(100.0)
        try:
            th.apply_fill(t, 101.0, 1.0, "oid2")
            assert False, "should have raised"
        except ValueError:
            pass

    def test_exit_computes_pnl(self):
        t = make_entered(100.0, qty=3.0)
        th.apply_exit(t, 106.0, "trailing_stop")
        assert t["status"] == "closed"
        assert abs(t["pnl_pct"] - 6.0) < 1e-6
        assert abs(t["pnl_dollars"] - 18.0) < 1e-6

    def test_exit_unknown_reason_refused(self):
        t = make_entered(100.0)
        try:
            th.apply_exit(t, 99.0, "vibes")
            assert False
        except ValueError:
            pass

    def test_terminal_only_from_active(self):
        t = th.build_thesis(raw_thesis(), TODAY)
        th.apply_terminal(t, "expired")
        assert t["status"] == "expired"
        t2 = make_entered(100.0)
        try:
            th.apply_terminal(t2, "cancelled")
            assert False
        except ValueError:
            pass

    def test_revision_records_old_values(self):
        t = make_entered(100.0)
        old_inval = t["invalidation_price"]
        th.apply_revision(t, {"invalidation_price": 95.0, "ttl_days": 8}, "tightening", TODAY)
        assert t["invalidation_price"] == 95.0
        assert t["ttl_days"] == 8
        assert t["expires"] == (TODAY.replace(day=25)).isoformat()
        assert t["revisions"][0]["changes"]["invalidation_price"] == [old_inval, 95.0]

    def test_revision_only_on_entered(self):
        t = th.build_thesis(raw_thesis(), TODAY)
        try:
            th.apply_revision(t, {"invalidation_price": 160}, "x", TODAY)
            assert False
        except ValueError:
            pass


class TestBreakerAndEvents:
    def test_daily_loss_threshold(self):
        assert th.daily_loss_halted(95.0, 100.0, -0.05) is True
        assert th.daily_loss_halted(95.01, 100.0, -0.05) is False
        assert th.daily_loss_halted(100.0, 0.0, -0.05) is False

    def test_event_shape(self):
        e = th.event("entry", thesis_id="T1", symbol="NVDA", qty=1.0)
        assert e["event"] == "entry" and "ts" in e and e["symbol"] == "NVDA"


class TestLessonsBounding:
    def test_truncates_oldest_keeps_newest(self, tmp_path):
        import trader_v2.store as store
        orig = store.LESSONS_FILE
        store.LESSONS_FILE = str(tmp_path / "lessons.md")
        try:
            for i in range(20):
                store.append_lesson(f"lesson number {i:02d} with padding text", "ev", "2026-07-17")
            text = store.read_lessons()   # bounded at 200 chars by env pin
            assert len(text) <= 200
            assert "lesson number 19" in text      # newest kept
            assert "lesson number 00" not in text  # oldest dropped
            assert all(line.startswith("- [") for line in text.splitlines())
        finally:
            store.LESSONS_FILE = orig

    def test_empty_file(self, tmp_path):
        import trader_v2.store as store
        orig = store.LESSONS_FILE
        store.LESSONS_FILE = str(tmp_path / "nope.md")
        try:
            assert store.read_lessons() == ""
        finally:
            store.LESSONS_FILE = orig
