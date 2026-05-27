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

TIMEFRAME: Intraday (5-minute bar intervals). All indicators computed from 5-minute bars.
- RSI(14) = 14-bar rolling RSI (≈70 minutes of data)
- MACD(8/21/5) = fast/slow/signal on 5-min bars (≈105-minute trend)
- Momentum(5) = % change over last 5 bars (≈25 minutes)
- Volume = 5-min bar volume vs 20-bar average

STRATEGY: MOMENTUM-ONLY (no mean-reversion). We trade trending stocks from the discovery feed. Do NOT buy oversold bounces or bounces off Bollinger lower bands.

BUY SIGNALS (Score >= {Config.TA_MIN_BUY_SCORE}):
- MACD histogram turning positive = momentum shift (+{Config.TA_MACD_WEIGHT} points)
- Positive momentum over last 5 bars (+{Config.TA_MOM_WEIGHT} points)
- Volume {Config.TA_VOL_THRESHOLD}x above average → score boosted by {Config.TA_VOL_BOOST}x
- Trend alignment (SMA 10 vs 20) (+{Config.TA_TREND_WEIGHT} points)

SELL SIGNALS (Score >= {Config.TA_MIN_SELL_SCORE}):
- RSI(14) above {Config.TA_RSI_OVERBOUGHT} = overextended momentum, take profits (+{Config.TA_RSI_WEIGHT} points)
- MACD histogram turning negative = momentum fading (+{Config.TA_MACD_WEIGHT} points)
- Price at/near Bollinger Band upper edge (+{Config.TA_BB_WEIGHT} points)
- Negative momentum over last 5 bars (+{Config.TA_MOM_WEIGHT} points)

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
      "strategy": "momentum|risk_management",
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
                    "momentum_5": data.get("momentum_5"),
                    "volume": data.get("volume"),
                    "buy_score": score.get("buy_score", 0),
                    "sell_score": score.get("sell_score", 0),
                    "meets_buy": score.get("meets_buy_threshold", False),
                    "meets_sell": score.get("meets_sell_threshold", False)
                }
        stock_deploy = portfolio.get("stock_deployment_current", 0)
        stock_deploy_max = portfolio.get("stock_deployment_max", 0)
        stock_deploy_pct = portfolio.get("stock_deployment_pct", 0)
        at_cap = portfolio.get("stock_deployment_at_cap", False)
        cap_warning = " — AT CAP, no new buys available" if at_cap else ""
        top_candidates = portfolio.get("top_candidates", [])
        news_context = portfolio.get("news_context", {})

        candidate_block = "No strong candidates this cycle."
        if top_candidates:
            candidate_data = {s: ta_formatted.get(s, {}) for s in top_candidates if s in ta_formatted}
            if candidate_data:
                candidate_block = json.dumps(candidate_data, indent=2)

        news_block = "None available"
        if news_context:
            news_block = json.dumps(news_context, indent=2)

        return f"""Portfolio:
- Total Value: ${portfolio['total_value']:.2f} | Cash: ${portfolio['cash']:.2f}
- Daily P&L: ${portfolio.get('daily_pl', 0):.2f} | Trades Today: {portfolio.get('trades_today', 0)}
- Stock Deployment: ${stock_deploy:.0f} of ${stock_deploy_max:.0f} cap ({stock_deploy_pct:.0f}%){cap_warning}

Current Positions:
{json.dumps(portfolio.get('positions', []), indent=2)}

Top Candidates (pre-scored by momentum, highest buy_score):
{candidate_block}

News & Catalysts:
{news_block}

Market Open: {portfolio.get('market_open', False)}
SPY Regime: {"BLOCKED (RSI < 30) — no new buys allowed" if portfolio.get("spy_regime_mode") == "blocked" else "REDUCED (RSI 30-40) — 50% position size, buy_score >= 3.0" if portfolio.get("spy_regime_mode") == "reduced" else "Normal (RSI >= 40) — trades permitted"}
SPY RSI(14): {portfolio.get("spy_rsi_14", "N/A")}

DECISION RULES:
1. BUY when buy_score >= {Config.TA_MIN_BUY_SCORE} (momentum confirmation signals)
2. SELL when sell_score >= {Config.TA_MIN_SELL_SCORE} (momentum fading or overextended)
3. Use stop loss at {Config.TA_STOP_LOSS_PCT:.0%} from entry price
4. Position size max ${account_value * Config.RISK_MAX_POSITION_PCT:.0f} ({Config.RISK_MAX_POSITION_PCT:.0%} of account)
5. All indicators are on 5-minute bars — MACD(8/21/5) captures ~105-min trends, momentum(5) ~25 min
6. Only trade stocks with clear momentum signals (no mean-reversion plays)
7. Evaluate each top candidate — consider news catalysts alongside TA signals

The buy_score and sell_score are pre-computed using the config weights. Use them as ground truth for your decisions. Focus on the top candidates above. You may also suggest trades from your current positions (sell decisions)."""
