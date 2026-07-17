"""Unit tests for pure, trivially-testable logic.

Covers:
- score_signals (trader/technical_analysis.py)
- _option_dte OSI parsing (options_bot/__main__.py)
- _get_dynamic_stop (options_bot/__main__.py)
- _max_position_size (trader/llm_engine.py)
"""

import sys
import os
from datetime import datetime, date

test_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, test_root)

# Force env vars before config.py loads .env — tests must be deterministic
os.environ["DATA_DIR"] = "/tmp/test_data"
os.environ["TA_MIN_BUY_SCORE"] = "1.0"
os.environ["TA_MIN_SELL_SCORE"] = "1.0"
os.environ["TA_RSI_OVERBOUGHT"] = "65"
os.environ["TA_RSI_WEIGHT"] = "1.0"
os.environ["TA_MACD_WEIGHT"] = "1.0"
os.environ["TA_BB_WEIGHT"] = "1.0"
os.environ["TA_BB_UPPER_THRESHOLD"] = "0.90"
os.environ["TA_TREND_WEIGHT"] = "1.0"
os.environ["TA_MOM_WEIGHT"] = "1.0"
os.environ["TA_MOM_THRESHOLD"] = "2.0"
os.environ["TA_VOL_THRESHOLD"] = "1.2"
os.environ["TA_VOL_BOOST"] = "1.2"
os.environ["TRADING_CAPITAL_ALLOCATION"] = "0.60"
os.environ["TARGET_POSITIONS"] = "10"
os.environ["RISK_STOP_LOSS_PCT"] = "-0.03"
os.environ["RISK_INTRADAY_STOP_PCT"] = "-0.06"
os.environ["RISK_CLOSE_WINDOW_MIN"] = "20"
os.environ["RISK_TRAIL_ACTIVATE_PCT"] = "0.03"
os.environ["RISK_TRAIL_STOP_PCT"] = "-0.03"

from trader.config import Config
from trader.technical_analysis import TechnicalAnalysis
from trader.llm_engine import _max_position_size, needs_llm_review
from trader.risk_manager import RiskManager, in_close_window

# Import options bot pure functions by patching heavy deps at module level
from unittest.mock import patch
with patch("alpaca.trading.client.TradingClient"), \
     patch("alpaca.data.StockHistoricalDataClient"), \
     patch("alpaca.data.OptionHistoricalDataClient"):
    from options_bot.__main__ import _option_dte, _get_dynamic_stop


class TestScoreSignals:
    """Deterministic buy/sell scoring — no Alpaca or LLM needed."""

    def test_macd_buy_signal(self):
        """MACD buy + trend + strong momentum → all components contribute."""
        ta = {
            "rsi_14": 50.0,
            "macd": {"histogram": 0.5, "trend": "bullish"},
            "bollinger_bands": {"position": 0.5},
            "sma_10": 110.0,
            "sma_20": 105.0,
            "momentum_5": 4.0,
            "volume": {"current": 50000, "ratio": 1.0},
        }
        result = TechnicalAnalysis.score_signals(ta)
        assert result["buy_score"] >= 1.0
        assert result["meets_buy_threshold"] is True
        assert result["components"]["macd_buy"] == 1.0
        assert result["components"]["trend_buy"] == 1.0

    def test_only_trend_alignment(self):
        """Only trend alignment (SMA10 > SMA20) → buy_score of 1.0."""
        ta = {
            "rsi_14": 50.0,
            "macd": {"histogram": -0.5, "trend": "bearish"},
            "bollinger_bands": {"position": 0.5},
            "sma_10": 110.0,
            "sma_20": 105.0,
            "momentum_5": 0.0,
            "volume": {"current": 50000, "ratio": 1.0},
        }
        result = TechnicalAnalysis.score_signals(ta)
        assert result["buy_score"] == 1.0
        assert result["meets_buy_threshold"] is True  # 1.0 >= Config.TA_MIN_BUY_SCORE (1.0)

    def test_all_signals_fire(self):
        """MACD + trend + momentum all confirm → buy_score ≈ 3.0 (or 3.6 with volume)."""
        ta = {
            "rsi_14": 50.0,
            "macd": {"histogram": 0.5, "trend": "bullish"},
            "bollinger_bands": {"position": 0.5},
            "sma_10": 110.0,
            "sma_20": 105.0,
            "momentum_5": 4.0,
            "volume": {"current": 50000, "ratio": 1.5},
        }
        result = TechnicalAnalysis.score_signals(ta)
        assert result["buy_score"] >= 3.0
        assert result["meets_buy_threshold"] is True

    def test_overbought_disqualifier(self):
        """RSI > 80 → buy_score forced to 0 regardless of signals."""
        ta = {
            "rsi_14": 82.0,
            "macd": {"histogram": 0.5, "trend": "bullish"},
            "bollinger_bands": {"position": 0.5},
            "sma_10": 110.0,
            "sma_20": 105.0,
            "momentum_5": 4.0,
            "volume": {"current": 50000, "ratio": 1.5},
        }
        result = TechnicalAnalysis.score_signals(ta)
        assert result["buy_score"] == 0.0
        assert result["meets_buy_threshold"] is False

    def test_overextended_rsi_disqualifier(self):
        """RSI above TA_RSI_BUY_MAX (default 72) → buy_score forced to 0."""
        ta = {
            "rsi_14": 74.0,
            "macd": {"histogram": 0.5, "trend": "bullish"},
            "bollinger_bands": {"position": 0.5},
            "sma_10": 110.0,
            "sma_20": 105.0,
            "momentum_5": 4.0,
            "volume": {"current": 50000, "ratio": 1.5},
        }
        result = TechnicalAnalysis.score_signals(ta)
        assert result["buy_score"] == 0.0
        assert result["overextended"] is True
        assert result["meets_buy_threshold"] is False

    def test_overextended_above_upper_band_disqualifier(self):
        """Price above the upper Bollinger band (position > TA_BB_BUY_MAX=1.0) → no buy."""
        ta = {
            "rsi_14": 60.0,
            "macd": {"histogram": 0.5, "trend": "bullish"},
            "bollinger_bands": {"position": 1.05},
            "sma_10": 110.0,
            "sma_20": 105.0,
            "momentum_5": 4.0,
            "volume": {"current": 50000, "ratio": 1.5},
        }
        result = TechnicalAnalysis.score_signals(ta)
        assert result["buy_score"] == 0.0
        assert result["overextended"] is True

    def test_overextended_day_gain_disqualifier(self):
        """Name already up > TA_MAX_DAY_GAIN_PCT (default 10%) on the day → no chase."""
        ta = {
            "rsi_14": 60.0,
            "price_change_pct": 13.1,
            "macd": {"histogram": 0.5, "trend": "bullish"},
            "bollinger_bands": {"position": 0.9},
            "sma_10": 110.0,
            "sma_20": 105.0,
            "momentum_5": 4.0,
            "volume": {"current": 50000, "ratio": 1.5},
        }
        result = TechnicalAnalysis.score_signals(ta)
        assert result["buy_score"] == 0.0
        assert result["overextended"] is True

    def test_not_overextended_allows_buy(self):
        """Strong-but-not-stretched momentum (RSI 60, mid-band, modest day gain) still buys."""
        ta = {
            "rsi_14": 60.0,
            "price_change_pct": 4.0,
            "macd": {"histogram": 0.5, "trend": "bullish"},
            "bollinger_bands": {"position": 0.7},
            "sma_10": 110.0,
            "sma_20": 105.0,
            "momentum_5": 4.0,
            "volume": {"current": 50000, "ratio": 1.5},
        }
        result = TechnicalAnalysis.score_signals(ta)
        assert result["buy_score"] >= 3.0
        assert result["overextended"] is False
        assert result["meets_buy_threshold"] is True

    def test_low_volume_disqualifier(self):
        """Volume < 1000 → buy_score forced to 0."""
        ta = {
            "rsi_14": 50.0,
            "macd": {"histogram": 0.5, "trend": "bullish"},
            "bollinger_bands": {"position": 0.5},
            "sma_10": 110.0,
            "sma_20": 105.0,
            "momentum_5": 4.0,
            "volume": {"current": 500, "ratio": 0.5},
        }
        result = TechnicalAnalysis.score_signals(ta)
        assert result["buy_score"] == 0.0
        assert result["meets_buy_threshold"] is False

    def test_sell_score_rsi_overbought(self):
        """RSI above overbought → sell_score > 0."""
        ta = {
            "rsi_14": 75.0,
            "macd": {"histogram": -0.5, "trend": "bearish"},
            "bollinger_bands": {"position": 0.95},
            "sma_10": 105.0,
            "sma_20": 110.0,
            "momentum_5": -3.0,
            "volume": {"current": 50000, "ratio": 1.0},
        }
        result = TechnicalAnalysis.score_signals(ta)
        assert result["sell_score"] > 0
        assert result["meets_sell_threshold"] is True

    def test_empty_ta_data_defaults(self):
        """Missing keys should default to neutral values without crashing."""
        result = TechnicalAnalysis.score_signals({})
        assert result["buy_score"] == 0.0
        assert result["sell_score"] == 0.0
        assert result["meets_buy_threshold"] is False
        assert result["meets_sell_threshold"] is False


class TestOptionDte:
    """OSI symbol parsing for days-to-expiration."""

    def test_valid_osi_parses_dte(self):
        """AAPL with far-future expiry should return positive DTE."""
        dte = _option_dte("AAPL300620C00150000")
        assert dte is not None
        assert dte > 0

    def test_expired_option_negative_dte(self):
        """Past expiry date should return negative DTE."""
        dte = _option_dte("AAPL200101C00150000")
        assert dte is not None
        assert dte < 0

    def test_invalid_symbol_returns_none(self):
        """Garbage symbol should return None, not crash."""
        dte = _option_dte("XYZ")
        assert dte is None

    def test_empty_string_returns_none(self):
        dte = _option_dte("")
        assert dte is None


class TestDynamicStop:
    """Dynamic stop-loss based on DTE."""

    def test_none_dte_returns_none(self):
        assert _get_dynamic_stop(None) is None

    def test_very_short_dte_stop(self):
        assert _get_dynamic_stop(3) == -25
        assert _get_dynamic_stop(5) == -25

    def test_medium_dte_stop(self):
        assert _get_dynamic_stop(7) == -40
        assert _get_dynamic_stop(14) == -40

    def test_long_dte_stop(self):
        assert _get_dynamic_stop(15) == -55
        assert _get_dynamic_stop(30) == -55


class TestMaxPositionSize:
    """Equity-scaled position sizing (env: TRADING_CAPITAL_ALLOCATION=0.60,
    TARGET_POSITIONS=10 base; defaults REF=$1000, +6/10×, MIN/MAX=5/25,
    HARD_CAP=25%, MIN_DOLLARS=$10). _max_position_size = equal-weight slice of
    trading capital (account × 0.60), NOT a hardcoded clamp."""

    def test_below_reference_uses_base_book(self):
        """$200 acct → tc=$120, book floored at base 10 → $120/10 = $12/pos."""
        assert round(_max_position_size(200), 2) == 12.0

    def test_medium_account_equal_weight(self):
        """$1000 acct → tc=$600, book=10 → $600/10 = $60/pos."""
        assert round(_max_position_size(1000), 2) == 60.0

    def test_large_account_scales_book_no_2000_clip(self):
        """$50k acct → tc=$30k, book=round(10+6·log10(30))=19 → $30000/19 ≈ $1578.95.
        Regression: the old hardcoded $2000 clamp is gone — size is the equal-weight
        slice, driven by the scaled book size."""
        assert round(_max_position_size(50000), 2) == round(30000 / 19, 2)

    def test_million_dollar_account_not_clipped(self):
        """$2M acct → tc=$1.2M, book clamped at MAX 25 → $1.2M/25 = $48,000/pos.
        Proves a large account deploys real size instead of the old $2k ceiling."""
        assert _max_position_size(2_000_000) == 48_000.0

    def test_zero_account_min_dollars(self):
        """$0 → POSITION_MIN_DOLLARS floor ($10)."""
        assert _max_position_size(0) == 10

    def test_negative_account_min_dollars(self):
        """Negative → POSITION_MIN_DOLLARS floor ($10)."""
        assert _max_position_size(-100) == 10


class TestEquityScaledFormulas:
    """Config.target_positions / max_trades_per_day scale with capital."""

    def test_book_grows_with_equity(self):
        assert Config.target_positions(1000) == 10        # base at reference
        assert Config.target_positions(10_000) == 16      # +6 per 10×
        assert Config.target_positions(100_000) == 22

    def test_book_clamped_at_max(self):
        assert Config.target_positions(1_000_000_000) == Config.TARGET_POSITIONS_MAX

    def test_book_floored_below_reference(self):
        assert Config.target_positions(50) == Config.TARGET_POSITIONS  # base, not lower

    def test_trades_per_day_scales_without_override(self):
        """No env pin → 2× the scaled book size."""
        with patch.object(Config, "_RISK_MAX_TRADES_OVERRIDE", None):
            assert Config.max_trades_per_day(1000) == 20      # 2 × 10
            assert Config.max_trades_per_day(100_000) == 44   # 2 × 22

    def test_explicit_pin_overrides_scaling(self):
        with patch.object(Config, "_RISK_MAX_TRADES_OVERRIDE", "12"):
            assert Config.max_trades_per_day(1_000_000) == 12


class TestNeedsLlmReview:
    """Idle-cycle gate: skip the LLM only when there's nothing to decide.
    buy_threshold=1.0, sell_threshold=1.0, near_stop_pct=-2.5 throughout."""

    BUY = 1.0
    SELL = 1.0
    NEAR = -2.5

    def _ta(self, buy=0.0, sell=0.0):
        return {"ABC": {"score": {"buy_score": buy, "sell_score": sell}}}

    def _pos(self, symbol="XYZ", mv=102.0, upl=2.0):
        # cost basis = mv - upl; pnl% = upl / cost * 100
        return {"symbol": symbol, "market_value": mv, "unrealized_pl": upl}

    def call(self, ta, positions, regime="normal"):
        return needs_llm_review(ta, positions, regime, self.BUY, self.SELL, self.NEAR)

    def test_buy_candidate_above_bar(self):
        assert self.call(self._ta(buy=1.5), []) is True

    def test_buy_candidate_below_bar(self):
        assert self.call(self._ta(buy=0.5), []) is False

    def test_fully_idle_is_skipped(self):
        # no buy candidate, one healthy position with a weak sell signal
        assert self.call(self._ta(buy=0.4), [self._pos(mv=105, upl=5)]) is False

    def test_position_approaching_stop(self):
        # pnl = -3% (<= -2.5) → review even with no buy candidate, no TA on the name
        assert self.call({}, [self._pos(mv=97, upl=-3)]) is True

    def test_position_just_above_near_stop(self):
        # pnl = -2.0% (> -2.5) → still idle
        assert self.call({}, [self._pos(mv=98, upl=-2)]) is False

    def test_position_sell_signal_fires(self):
        ta = {"XYZ": {"score": {"buy_score": 0.0, "sell_score": 1.2}}}
        assert self.call(ta, [self._pos(symbol="XYZ", mv=110, upl=10)]) is True

    def test_blocked_regime_ignores_buy_candidate(self):
        # SPY hard-block: a strong buy candidate must NOT force an LLM call
        assert self.call(self._ta(buy=5.0), [self._pos()], regime="blocked") is False

    def test_reduced_regime_raises_buy_bar_to_3(self):
        assert self.call(self._ta(buy=2.5), [], regime="reduced") is False   # below 3.0
        assert self.call(self._ta(buy=3.0), [], regime="reduced") is True    # meets 3.0

    def test_position_without_ta_uses_pnl_only(self):
        # held name absent from technical_analysis, healthy P&L → idle
        assert self.call(self._ta(buy=0.0), [self._pos(symbol="NOTA", mv=101, upl=1)]) is False


class TestTradingDaysHoldingPeriod:
    """Holding-period clock counts trading days, not calendar days
    (trader/risk_manager.py:trading_days_between)."""

    def test_friday_to_monday_is_one_trading_day(self):
        # Fri 2026-06-19 buy is NOT 3 days old on Mon 2026-06-22 (only 1 trading day)
        assert RiskManager.trading_days_between(date(2026, 6, 19), date(2026, 6, 22)) == 1

    def test_friday_buy_expires_wednesday(self):
        # Fri 6/19 → Wed 6/24 = Fri, Mon, Tue = 3 trading days → expires
        assert RiskManager.trading_days_between(date(2026, 6, 19), date(2026, 6, 24)) == 3

    def test_monday_to_thursday_is_three(self):
        assert RiskManager.trading_days_between(date(2026, 6, 15), date(2026, 6, 18)) == 3

    def test_same_day_is_zero(self):
        assert RiskManager.trading_days_between(date(2026, 6, 15), date(2026, 6, 15)) == 0

    def test_holiday_is_excluded(self):
        # Mon→Mon spans 5 weekdays; one holiday mid-week drops it to 4 trading days
        assert RiskManager.trading_days_between(
            date(2026, 6, 15), date(2026, 6, 22), holidays=[date(2026, 6, 17)]
        ) == 4

    def test_get_expired_uses_trading_days(self):
        # Build a risk manager with a Friday entry; "today" Monday → not expired (1 < 3)
        from types import SimpleNamespace
        rm = RiskManager.__new__(RiskManager)            # skip __init__/state load
        rm.market_holidays = []
        rm.position_trail = {}
        rm.position_entry_dates = {"AAA": datetime(2026, 6, 19, 14, 0)}
        # Monday is only 1 trading day later → not expired
        import trader.risk_manager as rmmod
        class _FixedDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 6, 22, 15, 0)
        orig = rmmod.datetime
        rmmod.datetime = _FixedDT
        try:
            assert rm.get_expired_positions([SimpleNamespace(symbol="AAA")], max_days=3) == []
            # …and on Wednesday (3 trading days) it expires
            _FixedDT2 = type("_F2", (datetime,), {"now": classmethod(lambda cls, tz=None: datetime(2026, 6, 24, 15, 0))})
            rmmod.datetime = _FixedDT2
            assert rm.get_expired_positions([SimpleNamespace(symbol="AAA")], max_days=3) == ["AAA"]
        finally:
            rmmod.datetime = orig


class TestCloseWindow:
    """in_close_window (trader/risk_manager.py) — the gate that defers the
    regular -3% stop to the last N minutes of the session. Clocks are faked
    with SimpleNamespace carrying tz-aware datetimes, like Alpaca's clock."""

    @staticmethod
    def _clock(is_open=True, minutes_to_close=30):
        from types import SimpleNamespace
        from datetime import timezone, timedelta as td
        now = datetime(2026, 7, 13, 15, 30, tzinfo=timezone.utc)
        return SimpleNamespace(
            is_open=is_open,
            timestamp=now,
            next_close=now + td(minutes=minutes_to_close),
        )

    def test_inside_window_true(self):
        assert in_close_window(self._clock(minutes_to_close=10), 20) is True

    def test_outside_window_false(self):
        assert in_close_window(self._clock(minutes_to_close=45), 20) is False

    def test_boundary_exact_window(self):
        # <= semantics: exactly window minutes out is inside
        assert in_close_window(self._clock(minutes_to_close=20), 20) is True

    def test_market_closed_false(self):
        assert in_close_window(self._clock(is_open=False, minutes_to_close=10), 20) is False

    def test_early_close_day(self):
        # 1pm-ET close: window math uses next_close, no hardcoded 16:00
        from types import SimpleNamespace
        from datetime import timezone, timedelta as td
        et = timezone(td(hours=-4))
        ts = datetime(2026, 11, 27, 12, 45, tzinfo=et)   # day after Thanksgiving
        clock = SimpleNamespace(is_open=True, timestamp=ts,
                                next_close=datetime(2026, 11, 27, 13, 0, tzinfo=et))
        assert in_close_window(clock, 20) is True

    def test_tz_mixed_offsets(self):
        # UTC timestamp vs ET next_close, same instant family — subtraction is tz-safe
        from types import SimpleNamespace
        from datetime import timezone, timedelta as td
        ts = datetime(2026, 7, 13, 19, 45, tzinfo=timezone.utc)          # 15:45 ET
        close = datetime(2026, 7, 13, 16, 0, tzinfo=timezone(td(hours=-4)))  # 16:00 ET
        clock = SimpleNamespace(is_open=True, timestamp=ts, next_close=close)
        assert in_close_window(clock, 20) is True

    def test_malformed_clock_fails_closed(self):
        from types import SimpleNamespace
        assert in_close_window(SimpleNamespace(is_open=True), 20) is False
        assert in_close_window(None, 20) is False
        assert in_close_window(SimpleNamespace(is_open=True, timestamp=None, next_close=None), 20) is False


class TestTwoTierStops:
    """check_stop_losses two-tier behavior: disaster stop every call, regular
    stop only when close_window=True."""

    @staticmethod
    def _rm(entry=100.0):
        rm = RiskManager.__new__(RiskManager)            # skip __init__/state load
        rm.positions = {
            "AAA": {
                "entry_price": entry,
                "stop_loss": entry * (1 + Config.RISK_STOP_LOSS_PCT),        # 97.0
                "disaster_stop": entry * (1 + Config.RISK_INTRADAY_STOP_PCT),  # 94.0
                "quantity": 1.0,
                "date": datetime(2026, 7, 10, 14, 0),
            }
        }
        return rm

    @staticmethod
    def _pos(price):
        from types import SimpleNamespace
        return [SimpleNamespace(symbol="AAA", last_price=price, qty=1.0)]

    def test_disaster_fires_outside_window(self):
        triggers = self._rm().check_stop_losses(self._pos(93.0), close_window=False)
        assert len(triggers) == 1
        assert triggers[0]["stop_type"] == "intraday_disaster"

    def test_regular_stop_deferred_outside_window(self):
        # -4%: below the regular stop, above disaster → held until the close window
        assert self._rm().check_stop_losses(self._pos(96.0), close_window=False) == []

    def test_regular_stop_fires_inside_window(self):
        triggers = self._rm().check_stop_losses(self._pos(96.0), close_window=True)
        assert len(triggers) == 1
        assert triggers[0]["stop_type"] == "close_stop"

    def test_no_double_trigger_in_window(self):
        # -7% inside the window breaches both levels → exactly one trigger (disaster)
        triggers = self._rm().check_stop_losses(self._pos(93.0), close_window=True)
        assert len(triggers) == 1
        assert triggers[0]["stop_type"] == "intraday_disaster"

    def test_healthy_position_never_triggers(self):
        assert self._rm().check_stop_losses(self._pos(99.0), close_window=False) == []
        assert self._rm().check_stop_losses(self._pos(99.0), close_window=True) == []

    def test_legacy_position_missing_disaster_key(self):
        # Positions registered before the disaster tier existed: recomputed, no crash
        rm = self._rm()
        del rm.positions["AAA"]["disaster_stop"]
        triggers = rm.check_stop_losses(self._pos(93.0), close_window=False)
        assert len(triggers) == 1
        assert triggers[0]["stop_type"] == "intraday_disaster"

    def test_position_price_fallback_chain(self):
        from types import SimpleNamespace
        assert RiskManager._position_price(SimpleNamespace(last_price=10.0)) == 10.0
        assert RiskManager._position_price(SimpleNamespace(current_price=11.0)) == 11.0
        assert RiskManager._position_price(SimpleNamespace(market_value=24.0, qty=2.0)) == 12.0
        assert RiskManager._position_price(SimpleNamespace(market_value=0.0, qty=0.0)) == 0.0


class TestStopPrices:
    def test_regular_stop_unchanged(self):
        rm = RiskManager.__new__(RiskManager)
        assert rm.get_stop_loss_price(100.0) == 97.0

    def test_disaster_stop_price(self):
        rm = RiskManager.__new__(RiskManager)
        assert rm.get_disaster_stop_price(100.0) == 94.0
        assert rm.get_disaster_stop_price(100.456) == round(100.456 * 0.94, 2)


class TestCancelOrdersForSymbol:
    """AlpacaClient.cancel_orders_for_symbol + the cancel-before-close ordering
    (a resting bracket/OTO stop leg holds the qty and rejects the close)."""

    @staticmethod
    def _client():
        from unittest.mock import MagicMock
        with patch("trader.alpaca_client.TradingClient"), \
             patch("trader.alpaca_client.StockHistoricalDataClient"):
            from trader.alpaca_client import AlpacaClient
            client = AlpacaClient()
        client.trading = MagicMock()
        return client

    def test_cancels_each_open_order(self):
        from types import SimpleNamespace
        client = self._client()
        client.trading.get_orders.return_value = [SimpleNamespace(id="o1"), SimpleNamespace(id="o2")]
        assert client.cancel_orders_for_symbol("AAA") == 2
        cancelled = [c.args[0] for c in client.trading.cancel_order_by_id.call_args_list]
        assert cancelled == ["o1", "o2"]
        req = client.trading.get_orders.call_args.args[0]
        assert req.symbols == ["AAA"]

    def test_close_position_cancels_first(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        client = self._client()
        manager = MagicMock()
        client.trading.get_orders.return_value = [SimpleNamespace(id="o1")]
        manager.attach_mock(client.trading.cancel_order_by_id, "cancel")
        manager.attach_mock(client.trading.close_position, "close")
        client.close_position("AAA")
        ops = [c[0] for c in manager.mock_calls]
        assert ops.index("cancel") < ops.index("close")

    def test_list_failure_returns_zero_and_does_not_raise(self):
        client = self._client()
        client.trading.get_orders.side_effect = RuntimeError("api down")
        assert client.cancel_orders_for_symbol("AAA") == 0


def _trail_rm(entry=100.0, hwm=None, trailing=False, entry_dt=None):
    """RiskManager with one tracked position AAA and controllable trail state.
    _save_state is stubbed out — persistence has its own tests below."""
    rm = RiskManager.__new__(RiskManager)
    rm.market_holidays = []
    rm.positions = {
        "AAA": {
            "entry_price": entry,
            "stop_loss": entry * (1 + Config.RISK_STOP_LOSS_PCT),
            "disaster_stop": entry * (1 + Config.RISK_INTRADAY_STOP_PCT),
            "quantity": 1.0,
            "date": datetime(2026, 7, 10, 14, 0),
        }
    }
    rm.position_entry_dates = {"AAA": entry_dt or datetime(2026, 7, 10, 14, 0)}
    rm.position_trail = {"AAA": {"hwm": hwm if hwm is not None else entry, "trailing": trailing}}
    rm._save_state = lambda: None
    return rm


def _mkpos(price, symbol="AAA"):
    from types import SimpleNamespace
    return SimpleNamespace(symbol=symbol, last_price=price, qty=1.0)


class TestTrailingStop:
    """update_trailing / check_trailing_stops / expiry exemption
    (trader/risk_manager.py). Defaults: activate +3%, trail -3% from hwm."""

    def test_hwm_ratchets_up_only(self):
        rm = _trail_rm()
        rm.update_trailing([_mkpos(104.0)])
        assert rm.position_trail["AAA"]["hwm"] == 104.0
        rm.update_trailing([_mkpos(102.0)])
        assert rm.position_trail["AAA"]["hwm"] == 104.0  # never moves down

    def test_trailing_activates_at_threshold(self):
        rm = _trail_rm()
        rm.update_trailing([_mkpos(102.9)])
        assert rm.position_trail["AAA"]["trailing"] is False
        rm.update_trailing([_mkpos(103.0)])          # exactly entry * 1.03
        assert rm.position_trail["AAA"]["trailing"] is True

    def test_trailing_sticky_after_decay(self):
        # Activated, then price falls back to entry → still trailing (the trail
        # exit will fire; it must not silently revert to expiry-governed)
        rm = _trail_rm(hwm=104.0, trailing=True)
        rm.update_trailing([_mkpos(100.0)])
        assert rm.position_trail["AAA"]["trailing"] is True

    def test_trailing_exit_fires_below_trail(self):
        rm = _trail_rm(hwm=110.0, trailing=True)
        triggers = rm.check_trailing_stops([_mkpos(106.6)])   # <= 110 * 0.97 = 106.7
        assert len(triggers) == 1
        assert triggers[0]["stop_type"] == "trailing"
        assert triggers[0]["hwm"] == 110.0

    def test_trailing_exit_holds_above_trail(self):
        rm = _trail_rm(hwm=110.0, trailing=True)
        assert rm.check_trailing_stops([_mkpos(106.8)]) == []

    def test_not_trailing_never_trail_exits(self):
        # Not activated: a -5% price is the (close-window) stop's job, not the trail's
        rm = _trail_rm(hwm=102.0, trailing=False)
        assert rm.check_trailing_stops([_mkpos(95.0)]) == []

    def test_untracked_symbol_skipped(self):
        # Symbol already force-closed this cycle (popped from positions) → no double-fire
        rm = _trail_rm(hwm=110.0, trailing=True)
        rm.positions.pop("AAA")
        assert rm.check_trailing_stops([_mkpos(100.0)]) == []

    def test_trailing_exempt_from_expiry(self):
        import trader.risk_manager as rmmod
        rm = _trail_rm(trailing=True, entry_dt=datetime(2026, 7, 6, 14, 0))
        _FixedDT = type("_F", (datetime,), {"now": classmethod(lambda cls, tz=None: datetime(2026, 7, 13, 15, 0))})
        orig = rmmod.datetime
        rmmod.datetime = _FixedDT
        try:
            # 5 trading days old but trailing → NOT expired
            assert rm.get_expired_positions([_mkpos(104.0)], max_days=3) == []
            # same clock, not trailing → expires (regression on existing behavior)
            rm.position_trail["AAA"]["trailing"] = False
            assert rm.get_expired_positions([_mkpos(104.0)], max_days=3) == ["AAA"]
        finally:
            rmmod.datetime = orig

    def test_register_topup_keeps_hwm_and_trailing(self):
        rm = _trail_rm(hwm=110.0, trailing=True)
        rm.register_position("AAA", 105.0, 2.0)      # top-up below the hwm
        assert rm.position_trail["AAA"]["hwm"] == 110.0
        assert rm.position_trail["AAA"]["trailing"] is True

    def test_register_new_position_initializes_trail(self):
        rm = _trail_rm()
        rm.position_trail = {}
        rm.register_position("BBB", 50.0, 1.0)
        assert rm.position_trail["BBB"] == {"hwm": 50.0, "trailing": False}

    def test_unregister_clears_trail_entry(self):
        rm = _trail_rm()
        rm.unregister_position("AAA")
        assert "AAA" not in rm.position_trail


class TestTrailStatePersistence:
    """position_trail round-trips through risk_state.json; legacy files load."""

    def _fresh_rm(self, tmpdir):
        import trader.risk_manager as rmmod
        orig = rmmod.STATE_FILE
        rmmod.STATE_FILE = os.path.join(tmpdir, "risk_state.json")
        return rmmod, orig

    def test_roundtrip_position_trail(self, tmp_path):
        import trader.risk_manager as rmmod
        orig = rmmod.STATE_FILE
        rmmod.STATE_FILE = str(tmp_path / "risk_state.json")
        try:
            rm = RiskManager()
            rm.position_trail = {"XYZ": {"hwm": 108.0, "trailing": True}}
            rm._save_state()
            rm2 = RiskManager()
            assert rm2.position_trail == {"XYZ": {"hwm": 108.0, "trailing": True}}
        finally:
            rmmod.STATE_FILE = orig

    def test_legacy_state_file_without_trail_key(self, tmp_path):
        import json as _json
        import trader.risk_manager as rmmod
        orig = rmmod.STATE_FILE
        rmmod.STATE_FILE = str(tmp_path / "risk_state.json")
        try:
            with open(rmmod.STATE_FILE, "w") as f:
                _json.dump({"daily_trades": 2, "position_entry_dates": {}}, f)
            rm = RiskManager()
            assert rm.position_trail == {}
            assert rm.daily_trades == 2
        finally:
            rmmod.STATE_FILE = orig


class TestSyncTrailMerge:
    """sync_from_alpaca must never clobber a persisted hwm, and must initialize
    untracked symbols at hwm = max(entry, current)."""

    @staticmethod
    def _alpaca_pos(symbol="AAA", entry=100.0, current=105.0):
        from types import SimpleNamespace
        return SimpleNamespace(symbol=symbol, avg_entry_price=entry, qty=1.0,
                               last_price=current)

    @staticmethod
    def _bare_rm():
        rm = RiskManager.__new__(RiskManager)
        rm.positions = {}
        rm.position_entry_dates = {}
        rm.position_trail = {}
        rm._save_state = lambda: None
        return rm

    def test_sync_preserves_persisted_hwm(self):
        # Restart scenario: trail state was persisted, positions dict was not
        rm = self._bare_rm()
        rm.position_trail = {"AAA": {"hwm": 108.0, "trailing": True}}
        rm.sync_from_alpaca([self._alpaca_pos(entry=100.0, current=105.5)])
        assert rm.position_trail["AAA"] == {"hwm": 108.0, "trailing": True}

    def test_sync_initializes_hwm_max_entry_current(self):
        rm = self._bare_rm()
        # dipped since entry → hwm floors at entry, not trailing
        rm.sync_from_alpaca([self._alpaca_pos(entry=100.0, current=96.0)])
        assert rm.position_trail["AAA"] == {"hwm": 100.0, "trailing": False}
        # up 7% since entry → hwm = current, trailing already active
        rm2 = self._bare_rm()
        rm2.sync_from_alpaca([self._alpaca_pos(entry=100.0, current=107.0)])
        assert rm2.position_trail["AAA"] == {"hwm": 107.0, "trailing": True}

    def test_sync_removes_trail_for_closed_symbols(self):
        rm = self._bare_rm()
        rm.position_trail = {"GONE": {"hwm": 50.0, "trailing": True}}
        rm.sync_from_alpaca([self._alpaca_pos(symbol="AAA")])
        assert "GONE" not in rm.position_trail
        assert "AAA" in rm.position_trail


class TestSpyBuyAndHold:
    """Deposit-dated SPY benchmark (trader/tracker.py). The old summary applied
    SPY's 250-day return to the whole balance — this replaces it."""

    CLOSES = {
        date(2026, 5, 7): 680.0,
        date(2026, 5, 8): 682.0,
        date(2026, 5, 11): 690.0,
        date(2026, 5, 18): 700.0,
        date(2026, 7, 15): 750.0,
    }

    def test_single_deposit(self):
        from trader.tracker import spy_buy_and_hold
        value, total = spy_buy_and_hold([(date(2026, 5, 7), 680.0)], self.CLOSES)
        assert total == 680.0
        assert value == 750.0  # exactly 1 share, valued at the last close

    def test_staged_deposits_weighted_by_entry_price(self):
        from trader.tracker import spy_buy_and_hold
        deps = [(date(2026, 5, 7), 200.0), (date(2026, 5, 11), 200.0), (date(2026, 5, 18), 950.0)]
        value, total = spy_buy_and_hold(deps, self.CLOSES)
        assert total == 1350.0
        expected = (200/680.0 + 200/690.0 + 950/700.0) * 750.0
        assert abs(value - expected) < 1e-9
        # later deposits at higher prices => less than "everything at day 1"
        assert value < (1350/680.0) * 750.0

    def test_weekend_deposit_buys_next_close(self):
        from trader.tracker import spy_buy_and_hold
        # 5/9 is between the 5/8 and 5/11 closes -> buys at 690, not 682
        value, _ = spy_buy_and_hold([(date(2026, 5, 9), 690.0)], self.CLOSES)
        assert value == 750.0

    def test_withdrawal_sells_shares(self):
        from trader.tracker import spy_buy_and_hold
        deps = [(date(2026, 5, 7), 680.0), (date(2026, 5, 18), -700.0)]  # buy 1 sh, sell 1 sh
        value, total = spy_buy_and_hold(deps, self.CLOSES)
        assert abs(value - 0.0) < 1e-9
        assert total == -20.0

    def test_deposit_after_last_close_carries_at_face_value(self):
        from trader.tracker import spy_buy_and_hold
        deps = [(date(2026, 5, 7), 680.0), (date(2026, 7, 16), 100.0)]
        value, total = spy_buy_and_hold(deps, self.CLOSES)
        assert value == 750.0 + 100.0
        assert total == 780.0

    def test_empty_inputs_fail_soft(self):
        from trader.tracker import spy_buy_and_hold
        assert spy_buy_and_hold([], self.CLOSES) == (0.0, 0.0)
        assert spy_buy_and_hold([(date(2026, 5, 7), 100.0)], {}) == (0.0, 0.0)
