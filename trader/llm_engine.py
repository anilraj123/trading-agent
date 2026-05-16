import json
import logging
from .config import Config

logger = logging.getLogger("trader.llm")


def _build_system_prompt(account_value: float = None) -> str:
    if account_value is None:
        account_value = Config.SIMULATED_ACCOUNT_SIZE
    stop_pct = abs(Config.TA_STOP_LOSS_PCT) * 100
    daily_loss_amt = abs(Config.RISK_DAILY_LOSS_LIMIT / 100 * account_value)
    max_pos = account_value * Config.RISK_MAX_POSITION_PCT

    return f"""You are an expert AI stock trading assistant. You analyze market data using TECHNICAL ANALYSIS and make precise trading decisions.

TIMEFRAME: Intraday (15-minute bar intervals). All indicators below are computed from minute-level bars.
- RSI(14) = 14-bar rolling RSI (≈3.5 hours of data)
- MACD = standard MACD on 15-min bars (≈2-3 hours of trend)
- Momentum(10) = % change over last 10 bars (≈2.5 hours)
- Volume = 15-min bar volume vs 20-bar average

ACTIVE STRATEGY CONFIG:

BUY SIGNALS (Score >= {Config.TA_MIN_BUY_SCORE}):
- RSI(14) below {Config.TA_RSI_OVERSOLD} = oversold bounce (+{Config.TA_RSI_WEIGHT} points)
- MACD histogram turning positive = momentum shift (+{Config.TA_MACD_WEIGHT} points)
- Price at/near Bollinger Band lower edge (+{Config.TA_BB_WEIGHT} points)
- Positive momentum over last 10 bars (+{Config.TA_MOM_WEIGHT} points)
- Volume {Config.TA_VOL_THRESHOLD}x above average → score boosted by {Config.TA_VOL_BOOST}x
- Trend alignment (SMA 10 vs 20) (+{Config.TA_TREND_WEIGHT} points)

SELL SIGNALS (Score >= {Config.TA_MIN_SELL_SCORE}):
- RSI(14) above {Config.TA_RSI_OVERBOUGHT} = take profits (+{Config.TA_RSI_WEIGHT} points)
- MACD histogram turning negative = momentum loss (+{Config.TA_MACD_WEIGHT} points)
- Price at/near Bollinger Band upper edge (+{Config.TA_BB_WEIGHT} points)
- Negative momentum over last 10 bars (+{Config.TA_MOM_WEIGHT} points)

RISK RULES:
- ${account_value:.0f} account. Max {Config.RISK_MAX_POSITION_PCT:.0%} (${max_pos:.0f}) per trade.
- Stop loss at {stop_pct:.0f}% from entry.
- Max {Config.RISK_MAX_TRADES_PER_DAY} trades per day.
- Daily loss limit: {abs(Config.RISK_DAILY_LOSS_LIMIT):.1f}% of account (${daily_loss_amt:.2f}).
- If uncertain, HOLD.

OUTPUT FORMAT (JSON only, no extra text):
{{
  "decisions": [
    {{
      "symbol": "AAPL",
      "action": "BUY|SELL|HOLD",
      "quantity": 0.5,
      "confidence": 0.85,
      "strategy": "mean_reversion|momentum|risk_management",
      "reasoning": "RSI at 22 (oversold) + positive MACD crossover suggests bounce"
    }}
  ],
  "market_outlook": "bullish|bearish|neutral",
  "summary": "Overall assessment in 1 sentence"
}}

Quantity: fractional shares allowed. Only include stocks with clear technical signals.
"""


class LLMEngine:
    def __init__(self):
        if Config.LLM_PROVIDER == "anthropic":
            from anthropic import Anthropic
            self.client = Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        else:
            from openai import OpenAI
            self.client = OpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=Config.LLM_API_KEY
            )

    def call(self, system=None, messages=None, model=None, max_tokens=1500, temperature=0.1):
        model = model or Config.LLM_MODEL
        if Config.LLM_PROVIDER == "anthropic":
            kwargs = dict(model=model, max_tokens=max_tokens, temperature=temperature)
            if system:
                kwargs["system"] = system
            kwargs["messages"] = messages or []
            response = self.client.messages.create(**kwargs)
            return response.content[0].text
        else:
            msgs = []
            if system:
                msgs.append({"role": "system", "content": system})
            msgs.extend(messages or [])
            response = self.client.chat.completions.create(
                model=model, messages=msgs, temperature=temperature, max_tokens=max_tokens
            )
            return response.choices[0].message.content.strip()

    def get_trading_decision(self, portfolio_data: dict, account_value: float = None) -> tuple:
        system_prompt = _build_system_prompt(account_value)
        user_prompt = self._build_prompt(portfolio_data, account_value)
        combined_prompt = f"SYSTEM:\n{system_prompt}\n\nUSER:\n{user_prompt}"

        raw_response = self.call(
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )

        try:
            parsed = raw_response
            start = parsed.find("{")
            end = parsed.rfind("}")
            if start != -1 and end != -1 and end > start:
                parsed = parsed[start:end+1]
            decision = json.loads(parsed)
            logger.info(f"LLM decision: {decision.get('market_outlook')} - {decision.get('summary')}")
            return decision, combined_prompt, raw_response
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM response: {raw_response[:200]}")
            return {
                "decisions": [],
                "market_outlook": "neutral",
                "summary": "Error parsing AI response",
                "error": str(e)
            }, combined_prompt, raw_response

    def _build_prompt(self, portfolio: dict, account_value: float = None) -> str:
        ta_data = portfolio.get("technical_analysis", {})
        ta_formatted = {}
        for symbol, data in ta_data.items():
            if data:
                score = data.get("score", {})
                ta_formatted[symbol] = {
                    "price": data.get("current_price"),
                    "change_pct": data.get("price_change_pct"),
                    "rsi_14": data.get("rsi_14"),
                    "macd": data.get("macd"),
                    "sma_10": data.get("sma_10"),
                    "sma_20": data.get("sma_20"),
                    "sma_50": data.get("sma_50"),
                    "bollinger": data.get("bollinger_bands"),
                    "momentum_10": data.get("momentum_10"),
                    "volume": data.get("volume"),
                    "buy_score": score.get("buy_score", 0),
                    "sell_score": score.get("sell_score", 0),
                    "meets_buy": score.get("meets_buy_threshold", False),
                    "meets_sell": score.get("meets_sell_threshold", False)
                }

        return f"""Portfolio:
- Total Value: ${portfolio['total_value']:.2f} | Cash: ${portfolio['cash']:.2f}
- Daily P&L: ${portfolio.get('daily_pl', 0):.2f} | Trades Today: {portfolio.get('trades_today', 0)}

Current Positions:
{json.dumps(portfolio.get('positions', []), indent=2)}

Technical Analysis for Watchlist ({len(ta_formatted)} stocks):
{json.dumps(ta_formatted, indent=2)}

Market Open: {portfolio.get('market_open', False)}

DECISION RULES:
1. BUY when buy_score >= {Config.TA_MIN_BUY_SCORE} (RSI < {Config.TA_RSI_OVERSOLD} + confirmations)
2. SELL when sell_score >= {Config.TA_MIN_SELL_SCORE} (RSI > {Config.TA_RSI_OVERBOUGHT} + confirmations)
3. Use stop loss at {Config.TA_STOP_LOSS_PCT:.0%} from entry price
4. Position size max ${account_value * Config.RISK_MAX_POSITION_PCT:.0f} ({Config.RISK_MAX_POSITION_PCT:.0%} of account)
5. All indicators are on 15-minute bars — momentum_10 = % change over ~2.5 hours
6. Only trade stocks with clear technical signals

The buy_score and sell_score are pre-computed using the config weights. Use them as ground truth for your decisions.

Provide decisions only for stocks meeting the buy/sell criteria."""
