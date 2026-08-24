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
        th.apply_fill(t, 175.0, 1.9, "oid", today=TODAY)
        assert t["status"] == "entered" and t["hwm"] == 175.0 and t["qty"] == 1.9

    def test_fill_restarts_ttl_clock(self):
        # thesis written Fri with 5-day TTL; filled 3 days later -> expires
        # ttl_days AFTER THE FILL, not after creation (the CSW 1-day artifact)
        t = th.build_thesis(raw_thesis(ttl_days=5), TODAY)
        assert t["expires"] == "2026-07-22"
        fill_day = date(2026, 7, 20)
        th.apply_fill(t, 175.0, 1.9, "oid", today=fill_day)
        assert t["expires"] == "2026-07-25"

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

# ---------------------------------------------------------------------------
# Schema v2: direction, catalyst_date, instrument policy (overhaul PR2)
# ---------------------------------------------------------------------------

def bearish_thesis(**over):
    base = dict(symbol="NVDA", direction="bearish", conviction=4,
                entry_zone_low=172.0, entry_zone_high=178.5,
                invalidation_price=185.0, price_target=155.0,
                catalyst="earnings miss 8/14", catalyst_date="2026-07-20",
                ttl_days=5, reasoning="test")
    base.update(over)
    return base


class TestDirectionValidation:
    def _ok(self, raw, close=175.0):
        return th.validate_new_thesis(raw, close, CANDIDATES, set(),
                                      today=TODAY, opt_min_conviction=4)

    def test_bullish_default_direction(self):
        t = th.build_thesis(raw_thesis(), TODAY)
        assert t["direction"] == "bullish" and t["multiplier"] == 1
        assert t["instrument"] is None and t["option"] is None

    def test_bad_direction_rejected(self):
        assert "bad direction" in self._ok(raw_thesis(direction="sideways"))

    def test_bearish_happy_path(self):
        assert self._ok(bearish_thesis()) is None

    def test_bearish_inverted_geometry_enforced(self):
        assert "must be above zone high" in self._ok(bearish_thesis(invalidation_price=170.0))
        assert "must be below zone low" in self._ok(bearish_thesis(price_target=180.0))
        assert "above zone" in self._ok(bearish_thesis(invalidation_price=210.0))  # >15% above

    def test_bearish_requires_conviction_bar(self):
        assert "conviction >= 4" in self._ok(bearish_thesis(conviction=3))

    def test_bearish_without_catalyst_date_ok(self):
        # The dated-catalyst hard gate was dropped when instrument choice moved
        # to the thesis: conviction is the bearish bar, catalyst is supporting.
        assert self._ok(bearish_thesis(catalyst_date=None)) is None

    def test_bad_instrument_rejected(self):
        assert "bad instrument" in self._ok(raw_thesis(instrument="futures"))

    def test_bearish_as_shares_rejected(self):
        assert "only expression" in self._ok(bearish_thesis(instrument="shares"))

    def test_bearish_builds_as_option_request(self):
        t = th.build_thesis(bearish_thesis(), TODAY)
        assert t["requested_instrument"] == "option"

    def test_bullish_default_requests_shares(self):
        assert th.build_thesis(raw_thesis(), TODAY)["requested_instrument"] == "shares"

    def test_bullish_option_request_carried(self):
        t = th.build_thesis(raw_thesis(instrument="option"), TODAY)
        assert t["requested_instrument"] == "option"

    def test_catalyst_date_unparseable(self):
        assert "unparseable" in self._ok(raw_thesis(catalyst_date="next tuesday"))

    def test_catalyst_date_outside_ttl_window(self):
        assert "outside" in self._ok(raw_thesis(catalyst_date="2026-09-01", ttl_days=5))
        assert "outside" in self._ok(raw_thesis(catalyst_date="2026-07-01"))  # in the past

    def test_catalyst_date_inside_window_ok(self):
        assert self._ok(raw_thesis(catalyst_date="2026-07-20", ttl_days=5)) is None

    def test_bullish_without_catalyst_date_still_fine(self):
        assert self._ok(raw_thesis()) is None


class TestChooseInstrument:
    KW = dict(opt_enabled=True, min_conviction=4, min_premium=50.0)

    def _active(self, **over):
        t = th.build_thesis(raw_thesis(instrument="option"), TODAY)
        t.update(over)
        return t

    def test_high_conviction_option_request_gets_option(self):
        inst, why = th.choose_instrument(self._active(), TODAY, 2, 300.0, **self.KW)
        assert (inst, why) == ("option", None)

    def test_disabled_falls_to_shares(self):
        kw = dict(self.KW, opt_enabled=False)
        assert th.choose_instrument(self._active(), TODAY, 2, 300.0, **kw) == ("shares", "options_disabled")

    def test_level_below_2(self):
        assert th.choose_instrument(self._active(), TODAY, 1, 300.0, **self.KW) == ("shares", "options_level")

    def test_conviction_below_bar(self):
        t = self._active(conviction=3)
        assert th.choose_instrument(t, TODAY, 2, 300.0, **self.KW) == ("shares", "conviction_below_bar")

    def test_shares_request_stays_shares(self):
        t = self._active(requested_instrument="shares")
        assert th.choose_instrument(t, TODAY, 2, 300.0, **self.KW) == ("shares", "not_requested")

    def test_legacy_thesis_without_request_field_is_shares(self):
        t = self._active()
        del t["requested_instrument"]
        assert th.choose_instrument(t, TODAY, 2, 300.0, **self.KW) == ("shares", "not_requested")

    def test_legacy_bearish_without_request_field_is_option(self):
        t = self._active(direction="bearish")
        del t["requested_instrument"]
        assert th.choose_instrument(t, TODAY, 2, 300.0, **self.KW) == ("option", None)

    def test_sleeve_full(self):
        assert th.choose_instrument(self._active(), TODAY, 2, 49.0, **self.KW) == ("shares", "sleeve_full")

    def test_bearish_skips_instead_of_shares(self):
        t = self._active(direction="bearish", conviction=3)
        assert th.choose_instrument(t, TODAY, 2, 300.0, **self.KW) == ("skip", "conviction_below_bar")

    def test_bearish_gets_put_policy_pass(self):
        t = self._active(direction="bearish")
        assert th.choose_instrument(t, TODAY, 2, 300.0, **self.KW) == ("option", None)


class TestSleeveRoom:
    def test_empty_book_full_room(self):
        assert th.sleeve_room([], 1000.0, 0.35) == 350.0

    def test_open_option_consumes(self):
        opt_pos = make_entered(entry=2.40, qty=1.0, instrument="option", multiplier=100)
        assert th.sleeve_room([opt_pos], 1000.0, 0.35) == 350.0 - 240.0

    def test_shares_do_not_consume(self):
        eq = make_entered(entry=100.0, qty=3.0, instrument="shares", multiplier=1)
        assert th.sleeve_room([eq], 1000.0, 0.35) == 350.0

    def test_floor_at_zero(self):
        opt_pos = make_entered(entry=5.0, qty=1.0, instrument="option", multiplier=100)
        assert th.sleeve_room([opt_pos], 1000.0, 0.35) == 0.0


class TestStoreMigration:
    def test_legacy_active_thesis_stamped(self):
        import trader_v2.store as store
        legacy = {"id": "T-X", "symbol": "NVDA", "status": "active"}
        m = store._migrate_thesis(dict(legacy))
        assert m["direction"] == "bullish" and m["multiplier"] == 1
        assert m["instrument"] is None and m["option"] is None and m["catalyst_date"] is None

    def test_legacy_entered_thesis_is_shares(self):
        import trader_v2.store as store
        m = store._migrate_thesis({"id": "T-X", "symbol": "NVDA", "status": "entered"})
        assert m["instrument"] == "shares"

    def test_migration_idempotent_preserves_v2_fields(self):
        import trader_v2.store as store
        t = {"id": "T-X", "symbol": "NVDA", "status": "entered",
             "direction": "bearish", "instrument": "option", "multiplier": 100,
             "option": {"contract": "NVDA260918P00170000", "type": "put"},
             "catalyst_date": "2026-08-14", "requested_instrument": "option"}
        m = store._migrate_thesis(dict(t))
        assert m == t

    def test_legacy_bearish_stamped_as_option_request(self):
        import trader_v2.store as store
        m = store._migrate_thesis({"id": "T-X", "symbol": "NVDA",
                                   "status": "active", "direction": "bearish"})
        assert m["requested_instrument"] == "option"

    def test_legacy_bullish_stamped_as_shares_request(self):
        import trader_v2.store as store
        m = store._migrate_thesis({"id": "T-X", "symbol": "NVDA", "status": "active"})
        assert m["requested_instrument"] == "shares"


class TestExitReasonsExtended:
    def test_option_reasons_accepted_by_apply_exit(self):
        for reason in ("premium_stop", "take_profit", "expiry_force_exit"):
            t = make_entered(entry=2.40, qty=1.0)
            th.apply_exit(t, 1.20, reason)
            assert t["exit_reason"] == reason

# ---------------------------------------------------------------------------
# Executor options integration: fills, P&L x100, reconcile (overhaul PR3)
# ---------------------------------------------------------------------------

OPTION_META = {"contract": "NVDA260918C00185000", "type": "call",
               "strike": 185.0, "expiry": "2026-09-18", "entry_delta": 0.52}


def make_entered_option(entry=2.40, qty=1.0, **over):
    t = th.build_thesis(raw_thesis(), TODAY)
    th.apply_fill(t, entry, qty, "ord1", instrument="option", option_meta=OPTION_META)
    t.update(over)
    return t


class TestOptionFill:
    def test_fill_sets_option_fields(self):
        t = make_entered_option()
        assert t["instrument"] == "option" and t["multiplier"] == 100
        assert t["option"]["contract"] == "NVDA260918C00185000"
        assert t["entry_price"] == 2.40 and t["qty"] == 1.0

    def test_option_fill_requires_meta(self):
        t = th.build_thesis(raw_thesis(), TODAY)
        try:
            th.apply_fill(t, 2.40, 1.0, "ord1", instrument="option")
            assert False, "should have raised"
        except ValueError as e:
            assert "option_meta" in str(e)

    def test_shares_fill_unchanged(self):
        t = th.build_thesis(raw_thesis(), TODAY)
        th.apply_fill(t, 100.0, 3.0, "ord1")
        assert t["instrument"] == "shares" and t["multiplier"] == 1 and t["option"] is None


class TestOptionPnl:
    def test_pnl_uses_100_multiplier(self):
        t = make_entered_option(entry=2.40, qty=2.0)
        th.apply_exit(t, 3.00, "take_profit")
        assert t["pnl_dollars"] == round((3.00 - 2.40) * 2 * 100, 4)  # $120
        assert t["pnl_pct"] == 25.0

    def test_shares_pnl_multiplier_1(self):
        t = make_entered(entry=100.0, qty=3.0)
        th.apply_exit(t, 110.0, "trailing_stop")
        assert t["pnl_dollars"] == 30.0

    def test_loss_pnl(self):
        t = make_entered_option(entry=2.40, qty=1.0)
        th.apply_exit(t, 1.20, "premium_stop")
        assert t["pnl_dollars"] == -120.0 and t["pnl_pct"] == -50.0


class TestBrokerSymbol:
    def test_option_uses_contract(self):
        assert th.broker_symbol(make_entered_option()) == "NVDA260918C00185000"

    def test_shares_uses_symbol(self):
        assert th.broker_symbol(make_entered()) == "NVDA"


class TestReconcileClassify:
    def test_mixed_book_all_matched(self):
        eq = make_entered(entry=100.0, qty=3.0)
        op = make_entered_option(qty=1.0)
        broker = {"NVDA": 3.0, "NVDA260918C00185000": 1.0}
        missing, drifted, untracked = th.reconcile_classify([eq, op], broker)
        assert missing == [] and drifted == [] and untracked == []

    def test_option_position_not_untracked(self):
        op = make_entered_option(qty=1.0)
        _, _, untracked = th.reconcile_classify([op], {"NVDA260918C00185000": 1.0})
        assert untracked == []

    def test_missing_option(self):
        op = make_entered_option()
        missing, _, _ = th.reconcile_classify([op], {})
        assert missing == [op]

    def test_qty_drift_option(self):
        op = make_entered_option(qty=2.0)
        _, drifted, _ = th.reconcile_classify([op], {"NVDA260918C00185000": 1.0})
        assert drifted == [(op, 1.0)]

    def test_truly_untracked_flagged(self):
        eq = make_entered(entry=100.0, qty=3.0)
        _, _, untracked = th.reconcile_classify(
            [eq], {"NVDA": 3.0, "TSLA": 5.0, "SPY260918P00500000": 1.0})
        assert set(untracked) == {"TSLA", "SPY260918P00500000"}

    def test_equity_same_underlying_as_option_is_distinct(self):
        # Holding NVDA shares while the thesis holds an NVDA call: the shares
        # are untracked (v2 didn't buy them), the call is matched.
        op = make_entered_option(qty=1.0)
        missing, drifted, untracked = th.reconcile_classify(
            [op], {"NVDA260918C00185000": 1.0, "NVDA": 10.0})
        assert missing == [] and drifted == [] and untracked == ["NVDA"]

# ---------------------------------------------------------------------------
# PDT guard — dormant on cash accounts, armed on margin (overhaul PR4)
# ---------------------------------------------------------------------------

from trader_v2 import guards


class TestPdtExitBlocked:
    TODAY_ISO = "2026-08-10"

    def test_multiday_hold_never_blocked(self):
        for reason in th.EXIT_REASONS:
            assert not guards.pdt_exit_blocked("2026-08-07", self.TODAY_ISO, 3, reason)

    def test_cash_account_null_count_never_blocked(self):
        assert not guards.pdt_exit_blocked(self.TODAY_ISO, self.TODAY_ISO, None, "trailing_stop")

    def test_noncritical_deferred_at_soft_max(self):
        for reason in ("trailing_stop", "take_profit", "research_close",
                       "ttl_expiry", "invalidation"):
            assert not guards.pdt_exit_blocked(self.TODAY_ISO, self.TODAY_ISO, 1, reason)
            assert guards.pdt_exit_blocked(self.TODAY_ISO, self.TODAY_ISO, 2, reason)

    def test_critical_allowed_through_third_blocked_at_fourth(self):
        for reason in guards.CRITICAL_EXITS:
            assert not guards.pdt_exit_blocked(self.TODAY_ISO, self.TODAY_ISO, 2, reason)
            assert guards.pdt_exit_blocked(self.TODAY_ISO, self.TODAY_ISO, 3, reason)

    def test_soft_max_configurable(self):
        assert not guards.pdt_exit_blocked(self.TODAY_ISO, self.TODAY_ISO, 2,
                                           "take_profit", soft_max=3)

    def test_entry_blocked_at_three(self):
        assert not guards.pdt_entry_blocked(2)
        assert guards.pdt_entry_blocked(3)
        assert not guards.pdt_entry_blocked(None)


class TestCapitalBase:
    """One capital anchor for the executor AND the analyst prompt. Live
    artifact (first night, 2026-08-21): research told the analyst $1350 while
    the dynamic-mode executor sized on $1032 of real equity."""

    def _mode(self, mode):
        from trader_v2.config import V2Config
        return V2Config, mode

    def test_dynamic_uses_equity(self, monkeypatch):
        from trader_v2.config import V2Config
        monkeypatch.setattr(V2Config, "CAPITAL_MODE", "dynamic")
        assert V2Config.capital_base(1031.95) == 1031.95

    def test_static_ignores_equity(self, monkeypatch):
        from trader_v2.config import V2Config
        monkeypatch.setattr(V2Config, "CAPITAL_MODE", "static")
        assert V2Config.capital_base(1031.95) == V2Config.CAPITAL

    def test_dynamic_falls_back_when_equity_missing(self, monkeypatch):
        from trader_v2.config import V2Config
        monkeypatch.setattr(V2Config, "CAPITAL_MODE", "dynamic")
        assert V2Config.capital_base(None) == V2Config.CAPITAL
        assert V2Config.capital_base(0) == V2Config.CAPITAL


class TestWeeklyCoverageNote:
    """The weekly reviewer must not read a fresh journal as a month of
    inactivity (first live weekly review minted a false lesson that way)."""

    CUTOFF = "2026-07-24T00:00:00Z"

    def test_empty_journal(self):
        from trader_v2.research import coverage_note
        note = coverage_note("", self.CUTOFF, 0)
        assert "EMPTY" in note and "Do not draw" in note

    def test_partial_coverage_fresh_journal(self):
        from trader_v2.research import coverage_note
        note = coverage_note("2026-08-21T22:00:00Z", self.CUTOFF, 2)
        assert "PART" in note and "inactivity" in note

    def test_full_coverage(self):
        from trader_v2.research import coverage_note
        note = coverage_note("2026-07-01T00:00:00Z", self.CUTOFF, 150)
        assert "full 28-day window" in note

    def test_journal_start_ts_empty_and_missing(self, tmp_path):
        import trader_v2.store as store
        orig = store.JOURNAL_FILE
        try:
            store.JOURNAL_FILE = str(tmp_path / "nope.jsonl")
            assert store.journal_start_ts() == ""
            p = tmp_path / "j.jsonl"
            p.write_text('{"ts": "2026-08-21T22:01:38Z", "event": "research_run"}\n'
                         '{"ts": "2026-08-21T23:00:00Z", "event": "heartbeat"}\n')
            store.JOURNAL_FILE = str(p)
            assert store.journal_start_ts() == "2026-08-21T22:01:38Z"
        finally:
            store.JOURNAL_FILE = orig


class TestResearchKeptCount:
    """`research_run.n_kept` is attribution data the weekly reviewer reads, so
    it must mean what it says. The old expression cancelled to len(entered) and
    reported every held position as kept even on close-everything nights."""

    def _entered(self, pending_close):
        t = th.build_thesis(raw_thesis(), TODAY)
        th.apply_fill(t, 100.0, 1.0, "oid", today=TODAY)
        t["pending_close"] = pending_close
        return t

    def test_empty_book(self):
        from trader_v2.research import kept_count
        assert kept_count([]) == 0

    def test_all_kept(self):
        from trader_v2.research import kept_count
        assert kept_count([self._entered(False), self._entered(False)]) == 2

    def test_close_everything_night(self):
        from trader_v2.research import kept_count
        assert kept_count([self._entered(True), self._entered(True)]) == 0

    def test_mixed(self):
        from trader_v2.research import kept_count
        assert kept_count([self._entered(True), self._entered(False)]) == 1

    def test_missing_flag_counts_as_kept(self):
        from trader_v2.research import kept_count
        t = self._entered(False)
        del t["pending_close"]
        assert kept_count([t]) == 1
