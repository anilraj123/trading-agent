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

os.environ.setdefault("DATA_DIR", "/tmp/test_data")
os.environ.setdefault("TA_MIN_BUY_SCORE", "3.0")
os.environ.setdefault("TA_MIN_SELL_SCORE", "1.0")
os.environ.setdefault("TA_RSI_OVERBOUGHT", "65")
os.environ.setdefault("TA_RSI_WEIGHT", "1.0")
os.environ.setdefault("TA_MACD_WEIGHT", "1.0")
os.environ.setdefault("TA_BB_WEIGHT", "1.0")
os.environ.setdefault("TA_BB_UPPER_THRESHOLD", "0.90")
os.environ.setdefault("TA_TREND_WEIGHT", "1.0")
os.environ.setdefault("TA_MOM_WEIGHT", "1.0")
os.environ.setdefault("TA_MOM_THRESHOLD", "2.0")
os.environ.setdefault("TA_VOL_THRESHOLD", "1.2")
os.environ.setdefault("TA_VOL_BOOST", "1.2")
os.environ.setdefault("TRADING_CAPITAL_ALLOCATION", "0.60")
os.environ.setdefault("TARGET_POSITIONS", "10")

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
        assert result["meets_buy_threshold"] is False  # 1.0 < Config.TA_MIN_BUY_SCORE (3.0)

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
    """Capital allocation math."""

    def test_small_account_floor(self):
        """$200 account → $50 floor (min position)."""
        size = _max_position_size(200)
        assert size == 50

    def test_medium_account_scales_up(self):
        """$1000 → $60 (above $50 floor)."""
        size = _max_position_size(1000)
        assert size == 60

    def test_large_account_hits_cap(self):
        """$50000 → $2000 cap."""
        size = _max_position_size(50000)
        assert size == 2000

    def test_zero_account(self):
        """$0 → $50 floor."""
        size = _max_position_size(0)
        assert size == 50

    def test_negative_account(self):
        """Negative → $50 floor."""
        size = _max_position_size(-100)
        assert size == 50
