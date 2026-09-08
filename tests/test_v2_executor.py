"""Unit tests for trader_v2/executor.py fill accounting.

Same conventions as the other v2 suites: env pinned before imports, no
network — the broker is a stub whose orders we drive by hand.

These guard the 2026-09-08 regression: a share market order comes back
ACCEPTED with filled_avg_price None, so reading the price off the submit
response booked the pre-trade QUOTE as the fill. Every share P&L, every
lesson minted from one, and the entry anchor of the disaster stop were wrong
(NBTX filled 44.79, booked 43.75, -8% stop consequently 2.4% too low).
"""
import sys
import os

test_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, test_root)

os.environ["DATA_DIR"] = "/tmp/test_data_v2"
os.environ["ALPACA_BASE_URL"] = "https://paper-api.alpaca.markets"
os.environ["V2_SHARE_FILL_WAIT_SEC"] = "1"      # keep the timeout test quick
os.environ["V2_SHARE_FILL_POLL_SEC"] = "0.01"
os.environ["V2_DISASTER_STOP_PCT"] = "-0.08"

from types import SimpleNamespace

from trader_v2 import executor as ex
from trader_v2.config import V2Config


class StubOrder:
    def __init__(self, order_id="o1"):
        self.id = order_id


class StubBroker:
    """Serves a scripted sequence of order states, one per get_order call;
    the last state repeats so a poll loop can spin on it."""

    def __init__(self, *states):
        self.states = list(states)
        self.polls = 0

    def get_order(self, order_id):
        self.polls += 1
        return self.states[min(self.polls - 1, len(self.states) - 1)]


def state(status, qty, avg):
    return SimpleNamespace(status=status, filled_qty=qty, filled_avg_price=avg)


ACCEPTED = state("OrderStatus.ACCEPTED", 0, None)


class TestStatusName:
    def test_unwraps_enum_repr(self):
        assert ex._status_name(state("OrderStatus.FILLED", 1, 1.0)) == "filled"

    def test_partial_is_not_filled(self):
        # The old endswith("filled")/startswith("partial") pair called this
        # complete, because the repr starts with "OrderStatus.".
        assert ex._status_name(state("OrderStatus.PARTIALLY_FILLED", 1, 1.0)) != "filled"

    def test_plain_string_status(self):
        assert ex._status_name(state("filled", 1, 1.0)) == "filled"

    def test_missing_status(self):
        assert ex._status_name(SimpleNamespace()) == ""


class TestWaitForFill:
    def test_returns_broker_average_not_the_quote(self):
        broker = StubBroker(state("OrderStatus.FILLED", 5.0, 44.7912))
        assert ex._wait_for_fill(broker, "o1", 1, poll_sec=0.01) == (5.0, 44.7912)

    def test_polls_until_filled(self):
        broker = StubBroker(ACCEPTED, ACCEPTED, state("OrderStatus.FILLED", 2.0, 10.5))
        assert ex._wait_for_fill(broker, "o1", 1, poll_sec=0.01) == (2.0, 10.5)
        assert broker.polls == 3

    def test_partial_does_not_return_early(self):
        broker = StubBroker(state("OrderStatus.PARTIALLY_FILLED", 3.0, 9.0))
        filled, avg = ex._wait_for_fill(broker, "o1", 1, poll_sec=0.01)
        assert broker.polls > 1              # kept waiting for the remainder
        assert (filled, avg) == (3.0, 9.0)   # then took the partial at timeout

    def test_timeout_with_nothing_filled(self):
        assert ex._wait_for_fill(StubBroker(ACCEPTED), "o1", 1, poll_sec=0.01) == (0.0, None)

    def test_poll_failures_do_not_raise(self):
        class Angry:
            def get_order(self, _):
                raise RuntimeError("api down")

        assert ex._wait_for_fill(Angry(), "o1", 1, poll_sec=0.01) == (0.0, None)


class TestShareFill:
    def test_books_the_fill_not_the_quote(self):
        """The regression itself: quote 43.75, true fill 44.7912."""
        broker = StubBroker(state("OrderStatus.FILLED", 5.9624, 44.7912))
        qty, price, estimated = ex._share_fill(broker, StubOrder(), 5.9624, 43.75)
        assert (qty, price) == (5.9624, 44.7912)
        assert estimated is False

    def test_adopts_the_brokers_qty(self):
        broker = StubBroker(state("OrderStatus.FILLED", 4.0, 20.0))
        qty, price, estimated = ex._share_fill(broker, StubOrder(), 7.0, 19.9)
        assert (qty, price, estimated) == (4.0, 20.0, False)

    def test_unfilled_falls_back_to_quote_and_flags_it(self):
        qty, price, estimated = ex._share_fill(StubBroker(ACCEPTED), StubOrder(), 3.0, 12.5)
        assert (qty, price) == (3.0, 12.5)
        assert estimated is True

    def test_missing_order_id_flags_estimated(self):
        qty, price, estimated = ex._share_fill(StubBroker(ACCEPTED), SimpleNamespace(), 3.0, 12.5)
        assert (qty, price, estimated) == (3.0, 12.5, True)

    def test_zero_avg_price_flags_estimated(self):
        # A filled qty with no average is not truth we can book.
        broker = StubBroker(state("OrderStatus.FILLED", 3.0, 0))
        qty, price, estimated = ex._share_fill(broker, StubOrder(), 3.0, 12.5)
        assert (qty, price, estimated) == (3.0, 12.5, True)

    def test_stop_anchors_on_the_true_entry(self):
        """The causal chain, as a regression test. NBTX's quote on 2026-09-01
        was 40.625: above the stop computed from the booked quote (so it never
        tripped, and the trade ran on to -12.9%), at-or-below the stop computed
        from the true fill (which would have cut it at -8%)."""
        broker = StubBroker(state("OrderStatus.FILLED", 5.9624, 44.7912))
        _, true_entry, estimated = ex._share_fill(broker, StubOrder(), 5.9624, 43.75)
        assert estimated is False
        price = 40.625
        assert price > 43.75 * (1 + V2Config.DISASTER_STOP_PCT)        # booked: no trip
        assert price <= true_entry * (1 + V2Config.DISASTER_STOP_PCT)  # true: trips


class TestConfig:
    def test_wait_and_poll_are_sane(self):
        assert 0 < V2Config.SHARE_FILL_POLL_SEC <= V2Config.SHARE_FILL_WAIT_SEC
