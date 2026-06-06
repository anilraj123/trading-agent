import json
import logging
from .config import Config

logger = logging.getLogger("trader.llm")

TRADING_DECISION_TOOL = {
    "name": "trading_decision",
    "description": "Trading decisions with market outlook",
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
                        "quantity": {"type": "number"},
                        "confidence": {"type": "number"},
                        "strategy": {"type": "string", "enum": ["momentum", "risk_management"]},
                        "reasoning": {"type": "string"}
                    },
                    "required": ["symbol", "action", "quantity", "confidence", "strategy", "reasoning"]
                }
            },
            "market_outlook": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
            "summary": {"type": "string"}
        },
        "required": ["decisions", "market_outlook", "summary"]
    }
}

OPTIONS_SIGNAL_TOOL = {
    "name": "options_signal",
    "description": "Options trading signal — pick one symbol or hold",
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "direction": {"type": "string", "enum": ["bullish", "bearish", "hold"]},
            "reasoning": {"type": "string"}
        },
        "required": ["symbol", "direction", "reasoning"]
    }
}


def _max_position_size(account_value: float) -> float:
    trading_capital = account_value * Config.TRADING_CAPITAL_ALLOCATION
    per_trade_size = trading_capital / Config.TARGET_POSITIONS
    return max(50, min(per_trade_size, 2000))


def _build_system_prompt(account_value: float = None) -> str:
    if account_value is None:
        account_value = Config.SIMULATED_ACCOUNT_SIZE
    stop_pct = abs(Config.TA_STOP_LOSS_PCT) * 100
    daily_loss_amt = abs(Config.RISK_DAILY_LOSS_LIMIT / 100 * account_value)
    max_pos = _max_position_size(account_value)

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
- ${account_value:.0f} account. Max ${max_pos:.0f} per trade ({100 / Config.TARGET_POSITIONS:.0f}% per position across {Config.TARGET_POSITIONS} target positions).
- Stop loss at {stop_pct:.0f}% from entry.
- Max {Config.RISK_MAX_TRADES_PER_DAY} trades per day.
- Daily loss limit: {abs(Config.RISK_DAILY_LOSS_LIMIT):.1f}% of account (${daily_loss_amt:.2f}).
- If uncertain, HOLD.

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

    def call_structured(self, tool_def, system=None, messages=None, model=None, max_tokens=1500, temperature=0.1):
        model = model or Config.LLM_MODEL
        if Config.LLM_PROVIDER == "anthropic":
            kwargs = dict(
                model=model, max_tokens=max_tokens, temperature=temperature,
                tools=[tool_def], tool_choice={"type": "tool", "name": tool_def["name"]}
            )
            if system:
                kwargs["system"] = system
            kwargs["messages"] = messages or []
            response = self.client.messages.create(**kwargs)
            for block in response.content:
                if block.type == "tool_use":
                    return block.input
            logger.error("No tool_use block in Anthropic response")
            return {"error": "No tool_use block in response"}
        else:
            msgs = []
            if system:
                msgs.append({"role": "system", "content": system + "\n\nRespond in JSON only."})
            msgs.extend(messages or [])
            response = self.client.chat.completions.create(
                model=model, messages=msgs, temperature=temperature, max_tokens=max_tokens
            )
            raw = response.choices[0].message.content.strip()
            try:
                start = raw.find("{")
                end = raw.rfind("}")
                if start != -1 and end != -1 and end > start:
                    raw = raw[start:end+1]
                return json.loads(raw)
            except json.JSONDecodeError:
                logger.error("Failed to parse OpenAI response: %s", raw[:200])
                return {"error": f"JSON parse failed: {raw[:100]}"}

    def get_trading_decision(self, portfolio_data: dict, account_value: float = None) -> tuple:
        system_prompt = _build_system_prompt(account_value)
        max_pos = _max_position_size(account_value or Config.SIMULATED_ACCOUNT_SIZE)
        user_prompt = self._build_prompt(portfolio_data, account_value, max_pos)
        combined_prompt = f"SYSTEM:\n{system_prompt}\n\nUSER:\n{user_prompt}"

        decision = self.call_structured(
            TRADING_DECISION_TOOL,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )

        if decision.get("error"):
            logger.error("LLM decision error: %s", decision["error"])
            return {
                "decisions": [], "market_outlook": "neutral",
                "summary": "Error parsing AI response", "error": decision["error"]
            }, combined_prompt, str(decision)

        logger.info("LLM decision: %s - %s", decision.get("market_outlook"), decision.get("summary"))
        return decision, combined_prompt, json.dumps(decision)

    def _build_prompt(self, portfolio: dict, account_value: float = None, max_pos: float = None) -> str:
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

        stock_positions = [p for p in portfolio.get('positions', []) if len(p.get('symbol', '')) <= 10]
        if max_pos is None:
            max_pos = _max_position_size(account_value or Config.SIMULATED_ACCOUNT_SIZE)

        return f"""Portfolio:
- Total Value: ${portfolio['total_value']:.2f} | Cash: ${portfolio['cash']:.2f}
- Daily P&L: ${portfolio.get('daily_pl', 0):.2f} | Trades Today: {portfolio.get('trades_today', 0)}
- Stock Deployment: ${stock_deploy:.0f} of ${stock_deploy_max:.0f} cap ({stock_deploy_pct:.0f}%){cap_warning}

Current Positions:
{json.dumps(stock_positions, indent=2)}

Top Candidates (pre-scored by momentum, highest buy_score):
{candidate_block}

News & Catalysts:
{news_block}

Market Open: {portfolio.get('market_open', False)}
SPY Regime: {"BLOCKED (RSI < 30) — no new buys allowed" if portfolio.get("spy_regime_mode") == "blocked" else "REDUCED (RSI 30-40) — 50% position size, buy_score >= 3.0" if portfolio.get("spy_regime_mode") == "reduced" else "Normal (RSI >= 40) — trades permitted"}
SPY RSI(14): {portfolio.get("spy_rsi_14", "N/A")}

DECISION RULES:
1. BUY when buy_score >= {Config.TA_MIN_BUY_SCORE} (momentum confirmation signals)
2. SELL: do not recommend SELL on a position unless EITHER:
   a. sell_score >= {Config.TA_MIN_SELL_SCORE:.1f} (deterministic signal), OR
   b. unrealized P&L <= -2.5% (approaching stop), OR
   c. DTE-based rule applies (options only)
3. Use stop loss at {Config.TA_STOP_LOSS_PCT:.0%} from entry price
4. Position size max ${max_pos:.0f} per trade (target {Config.TARGET_POSITIONS} concurrent positions)
5. All indicators are on 5-minute bars — MACD(8/21/5) captures ~105-min trends, momentum(5) ~25 min
6. Only trade stocks with clear momentum signals (no mean-reversion plays)
7. Evaluate each top candidate — consider news catalysts alongside TA signals

The buy_score and sell_score are pre-computed using the config weights. Use them as ground truth for your decisions. Focus on the top candidates above. You may also suggest trades from your current positions (sell decisions)."""
