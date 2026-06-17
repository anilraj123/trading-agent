"""Unit tests for pure, trivially-testable logic.

Covers:
- score_signals (trader/technical_analysis.py)
- _option_dte OSI parsing (options_bot/__main__.py)
- _get_dynamic_stop (options_bot/__main__.py)
- _max_position_size (trader/llm_engine.py)
"""

import sys
import os

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

from trader.config import Config
from trader.technical_analysis import TechnicalAnalysis
from trader.llm_engine import _max_position_size

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
