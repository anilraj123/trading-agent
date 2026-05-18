import numpy as np
import pandas as pd
from typing import Optional
from .config import Config

class TechnicalAnalysis:
    @staticmethod
    def rsi(close_prices: pd.Series, period: int = 14) -> float:
        delta = close_prices.diff()
        gain = delta.where(delta > 0, 0).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss.replace(0, np.nan)
        return round(100 - (100 / (1 + rs.iloc[-1])), 2) if pd.notna(rs.iloc[-1]) else 50.0

    @staticmethod
    def macd(close_prices: pd.Series, fast: int = 8, slow: int = 21, signal: int = 5) -> dict:
        ema_fast = close_prices.ewm(span=fast, adjust=False).mean()
        ema_slow = close_prices.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line

        return {
            "macd": float(round(macd_line.iloc[-1], 4)),
            "signal": float(round(signal_line.iloc[-1], 4)),
            "histogram": float(round(histogram.iloc[-1], 4)),
            "trend": "bullish" if macd_line.iloc[-1] > signal_line.iloc[-1] else "bearish"
        }

    @staticmethod
    def sma(close_prices: pd.Series, period: int = 20) -> float:
        return round(close_prices.rolling(window=period).mean().iloc[-1], 2)

    @staticmethod
    def ema(close_prices: pd.Series, period: int = 20) -> float:
        return round(close_prices.ewm(span=period, adjust=False).mean().iloc[-1], 2)

    @staticmethod
    def bollinger_bands(close_prices: pd.Series, period: int = 20, std_dev: float = 2.0) -> dict:
        sma = close_prices.rolling(window=period).mean()
        std = close_prices.rolling(window=period).std()
        upper = sma + (std * std_dev)
        lower = sma - (std * std_dev)
        bandwidth = (upper.iloc[-1] - lower.iloc[-1]) / sma.iloc[-1]

        return {
            "upper": float(round(upper.iloc[-1], 2)),
            "middle": float(round(sma.iloc[-1], 2)),
            "lower": float(round(lower.iloc[-1], 2)),
            "bandwidth": float(round(bandwidth, 4)),
            "position": float(round((close_prices.iloc[-1] - lower.iloc[-1]) / (upper.iloc[-1] - lower.iloc[-1]), 4))
        }

    @staticmethod
    def atr(high_prices: pd.Series, low_prices: pd.Series, close_prices: pd.Series, period: int = 14) -> float:
        prev_close = close_prices.shift(1)
        tr1 = high_prices - low_prices
        tr2 = (high_prices - prev_close).abs()
        tr3 = (low_prices - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return round(tr.rolling(window=period).mean().iloc[-1], 2)

    @staticmethod
    def volume_analysis(volume: pd.Series, price_change_pct: float) -> dict:
        avg_volume = volume.rolling(window=20).mean().iloc[-1]
        current_volume = volume.iloc[-1]
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0

        signal = "neutral"
        if volume_ratio > 2.0 and price_change_pct > 0:
            signal = "strong_buy_volume"
        elif volume_ratio > 2.0 and price_change_pct < 0:
            signal = "strong_sell_volume"
        elif volume_ratio > 1.5:
            signal = "elevated_volume"

        return {
            "current": int(current_volume),
            "avg_20": int(avg_volume),
            "ratio": float(round(volume_ratio, 2)),
            "signal": signal
        }

    @staticmethod
    def momentum(close_prices: pd.Series, period: int = 5) -> float:
        return round(((close_prices.iloc[-1] / close_prices.iloc[-period] - 1) * 100), 2) if len(close_prices) >= period else 0.0

    @staticmethod
    def score_signals(ta_data: dict) -> dict:
        """Compute deterministic buy/sell scores from config weights.

        Uses graded scoring — stronger signals get higher scores rather than
        all-or-nothing binary triggers.

        Returns {"buy_score": float, "sell_score": float, "components": {...}}
        """
        components = {}

        rsi = ta_data.get("rsi_14", 50.0)
        rsi_os = Config.TA_RSI_OVERSOLD
        rsi_ob = Config.TA_RSI_OVERBOUGHT
        if rsi < rsi_os:
            rsi_buy_grade = max(0.0, min(1.0, (rsi_os - rsi) / rsi_os))
        else:
            rsi_buy_grade = 0.0
        if rsi > rsi_ob:
            rsi_sell_grade = max(0.0, min(1.0, (rsi - rsi_ob) / (100 - rsi_ob)))
        else:
            rsi_sell_grade = 0.0
        components["rsi_buy"] = rsi_buy_grade * Config.TA_RSI_WEIGHT
        components["rsi_sell"] = rsi_sell_grade * Config.TA_RSI_WEIGHT

        macd = ta_data.get("macd", {})
        macd_hist = macd.get("histogram", 0) if isinstance(macd, dict) else 0
        macd_trend = macd.get("trend", "bearish") if isinstance(macd, dict) else "bearish"
        macd_buy = 1.0 if macd_hist > 0 and macd_trend == "bullish" else 0.0
        macd_sell = 1.0 if macd_hist < 0 and macd_trend == "bearish" else 0.0
        components["macd_buy"] = macd_buy * Config.TA_MACD_WEIGHT
        components["macd_sell"] = macd_sell * Config.TA_MACD_WEIGHT

        bb = ta_data.get("bollinger_bands", {})
        bb_pos = bb.get("position", 0.5) if isinstance(bb, dict) else 0.5
        bb_lower = Config.TA_BB_LOWER_THRESHOLD
        bb_upper = Config.TA_BB_UPPER_THRESHOLD
        if bb_pos < bb_lower:
            bb_buy_grade = max(0.0, min(1.0, (bb_lower - bb_pos) / bb_lower))
        else:
            bb_buy_grade = 0.0
        if bb_pos > bb_upper:
            bb_sell_grade = max(0.0, min(1.0, (bb_pos - bb_upper) / (1 - bb_upper)))
        else:
            bb_sell_grade = 0.0
        components["bb_buy"] = bb_buy_grade * Config.TA_BB_WEIGHT
        components["bb_sell"] = bb_sell_grade * Config.TA_BB_WEIGHT

        sma_10 = ta_data.get("sma_10", None)
        sma_20 = ta_data.get("sma_20", None)
        trend_buy = 1.0 if sma_10 is not None and sma_20 is not None and sma_10 > sma_20 else 0.0
        trend_sell = 1.0 if sma_10 is not None and sma_20 is not None and sma_10 < sma_20 else 0.0
        components["trend_buy"] = trend_buy * Config.TA_TREND_WEIGHT
        components["trend_sell"] = trend_sell * Config.TA_TREND_WEIGHT

        mom = ta_data.get("momentum_5", 0.0)
        mom_thresh = Config.TA_MOM_THRESHOLD
        if mom > mom_thresh:
            mom_buy_grade = max(0.0, min(1.0, (mom - mom_thresh) / mom_thresh))
        else:
            mom_buy_grade = 0.0
        if mom < -mom_thresh:
            mom_sell_grade = max(0.0, min(1.0, (abs(mom) - mom_thresh) / mom_thresh))
        else:
            mom_sell_grade = 0.0
        components["momentum_buy"] = mom_buy_grade * Config.TA_MOM_WEIGHT
        components["momentum_sell"] = mom_sell_grade * Config.TA_MOM_WEIGHT

        vol = ta_data.get("volume", {})
        vol_ratio = vol.get("ratio", 1.0) if isinstance(vol, dict) else 1.0
        vol_boost = Config.TA_VOL_BOOST if vol_ratio > Config.TA_VOL_THRESHOLD else 1.0
        components["volume_mult"] = vol_boost

        buy_score = (components["rsi_buy"] + components["macd_buy"] +
                     components["bb_buy"] + components["trend_buy"] +
                     components["momentum_buy"]) * vol_boost

        sell_score = (components["rsi_sell"] + components["macd_sell"] +
                      components["bb_sell"] + components["trend_sell"] +
                      components["momentum_sell"]) * vol_boost

        return {
            "buy_score": float(round(buy_score, 2)),
            "sell_score": float(round(sell_score, 2)),
            "meets_buy_threshold": bool(buy_score >= Config.TA_MIN_BUY_SCORE),
            "meets_sell_threshold": bool(sell_score >= Config.TA_MIN_SELL_SCORE),
            "components": {k: float(round(v, 3)) for k, v in components.items()}
        }

    @staticmethod
    def compute_all(df: pd.DataFrame) -> dict:
        close = df['close']
        high = df['high']
        low = df['low']
        volume = df['volume']

        price_change_pct = ((close.iloc[-1] / close.iloc[-2] - 1) * 100) if len(close) > 1 else 0.0

        result = {
            "current_price": float(round(close.iloc[-1], 2)),
            "price_change_pct": float(round(price_change_pct, 2)),
            "rsi_14": float(TechnicalAnalysis.rsi(close, 14)),
            "macd": TechnicalAnalysis.macd(close),
            "sma_10": float(TechnicalAnalysis.sma(close, 10)),
            "sma_20": float(TechnicalAnalysis.sma(close, 20)),
            "sma_50": float(TechnicalAnalysis.sma(close, 50)) if len(close) >= 50 else None,
            "ema_12": float(TechnicalAnalysis.ema(close, 12)),
            "ema_26": float(TechnicalAnalysis.ema(close, 26)),
            "bollinger_bands": TechnicalAnalysis.bollinger_bands(close),
            "atr_14": float(TechnicalAnalysis.atr(high, low, close, 14)),
            "volume": TechnicalAnalysis.volume_analysis(volume, price_change_pct),
            "momentum_5": float(TechnicalAnalysis.momentum(close, 5))
        }

        result["score"] = TechnicalAnalysis.score_signals(result)
        return result
