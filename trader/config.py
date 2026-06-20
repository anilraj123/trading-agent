import math
import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
    ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
    ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic")
    LLM_API_KEY = os.getenv("LLM_API_KEY")
    LLM_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-4-6")
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

    # 1.0 = equity bot uses the full trading slice (options bot paused 2026-06-09).
    # Was 0.60 when capital was split 60/40 with the options bot. If options is
    # re-enabled, lower this so both bots' allocations sum to <= 1.0.
    TRADING_CAPITAL_ALLOCATION = float(os.getenv("TRADING_CAPITAL_ALLOCATION", "1.0"))
    # TARGET_POSITIONS is now the BASE book size at TARGET_POSITIONS_REF_CAPITAL,
    # not a fixed count. The live book size grows with equity via target_positions()
    # below — a fixed count over-concentrates as equity scales (8 names × $125k at
    # $1M). Call sites should use Config.target_positions(trading_capital), NOT this
    # raw attribute, for anything equity-sensitive. Kept as an attribute for the
    # baseline/display and as the formula's anchor.
    TARGET_POSITIONS = int(os.getenv("TARGET_POSITIONS", "8"))
    RISK_DAILY_LOSS_LIMIT = float(os.getenv("RISK_DAILY_LOSS_LIMIT", "-5.00"))
    # Stop-loss fraction (negative). Falls back to legacy TA_STOP_LOSS_PCT for
    # backward compatibility with existing .env files; RISK_STOP_LOSS_PCT wins if set.
    RISK_STOP_LOSS_PCT = float(os.getenv("RISK_STOP_LOSS_PCT", os.getenv("TA_STOP_LOSS_PCT", "-0.03")))
    # Voluntary-trade circuit breaker (forced exits — stops/expiry — don't count).
    # Defaults to 2x TARGET_POSITIONS so the bot can fully establish OR rotate its
    # book in a day (sell all + buy all) without freezing mid-rebalance, while still
    # tripping on a genuine runaway loop. The old flat 5 was sized for the 5-min
    # churn era and throttled legitimate daily rebalancing. Env var still overrides.
    # If RISK_MAX_TRADES_PER_DAY is explicitly pinned in the env it is a MANUAL
    # override (a flat cap). Otherwise the live cap is RISK_MAX_TRADES_MULT × the
    # equity-scaled book size — see max_trades_per_day(). _OVERRIDE is None when
    # unpinned; RISK_MAX_TRADES_PER_DAY stays as the baseline for display/fallback.
    _RISK_MAX_TRADES_OVERRIDE = os.getenv("RISK_MAX_TRADES_PER_DAY")
    RISK_MAX_TRADES_MULT = int(os.getenv("RISK_MAX_TRADES_MULT", "2"))
    RISK_MAX_TRADES_PER_DAY = int(_RISK_MAX_TRADES_OVERRIDE) if _RISK_MAX_TRADES_OVERRIDE else TARGET_POSITIONS * RISK_MAX_TRADES_MULT

    # --- Equity-scaled position sizing -------------------------------------
    # Percentages are scale-invariant, but a fixed book size and a hardcoded
    # dollar position cap break as equity grows: at $1M, the old
    # max(50, min(slice, 2000)) clamp clipped every order to $2,000, deploying
    # ~$16k of $1M. So the book size scales logarithmically with trading capital
    # and the per-position cap is an equal-weight slice with a *percentage* hard
    # cap — no absolute dollar ceiling. All knobs are env-tunable.
    TARGET_POSITIONS_REF_CAPITAL = float(os.getenv("TARGET_POSITIONS_REF_CAPITAL", "1000"))  # equity where book = TARGET_POSITIONS
    TARGET_POSITIONS_PER_10X = float(os.getenv("TARGET_POSITIONS_PER_10X", "6"))             # +N names per 10× capital
    TARGET_POSITIONS_MIN = int(os.getenv("TARGET_POSITIONS_MIN", "5"))
    TARGET_POSITIONS_MAX = int(os.getenv("TARGET_POSITIONS_MAX", "25"))                       # < ranked-watchlist depth (30)
    POSITION_HARD_CAP_PCT = float(os.getenv("POSITION_HARD_CAP_PCT", "0.25"))                 # no single position > this fraction of capital
    POSITION_MIN_DOLLARS = float(os.getenv("POSITION_MIN_DOLLARS", "10"))                     # floor so tiny accounts can still place an order
    RISK_MAX_HOLDING_DAYS = int(os.getenv("RISK_MAX_HOLDING_DAYS", "3"))
    # Minimum days a position must be held before a VOLUNTARY (LLM-driven) sell is
    # allowed. Hard stops (RISK_STOP_LOSS_PCT) and expiry exits ignore this. The
    # edge was validated on DAILY bars held for days, but the TA is cached once per
    # day, so any same-day sell-after-buy is the LLM flip-flopping on intraday price
    # noise against an unchanged signal (see Jun 10 churn: DHR/TJX round-tripped in
    # <30 min). 1 = can't voluntarily exit until at least the next session.
    RISK_MIN_HOLDING_DAYS = int(os.getenv("RISK_MIN_HOLDING_DAYS", "1"))
    RISK_MIN_CONFIDENCE = float(os.getenv("RISK_MIN_CONFIDENCE", "0.6"))

    TRADING_INTERVAL_MINUTES = int(os.getenv("TRADING_INTERVAL_MINUTES", "15"))
    WATCHLIST = [s.strip() for s in os.getenv("WATCHLIST", "AAPL,MSFT,TSLA,SPY,QQQ").split(",")]
    BLACKLIST = [s.strip().upper() for s in os.getenv("BLACKLIST", "").split(",") if s.strip()]
    CRYPTO_SUFFIXES = [s.upper() for s in os.getenv("CRYPTO_SUFFIXES", "USD,USDT,USDC,BTC").split(",") if s.strip()]
    SIMULATED_ACCOUNT_SIZE = float(os.getenv("SIMULATED_ACCOUNT_SIZE", "200"))
    # 0.95 = deploy nearly all cash into stocks (small buffer for fees/slippage).
    # Was 0.50 to reserve half the cash as an options buying-power buffer; with the
    # options bot paused that reservation is no longer needed.
    MAX_STOCK_DEPLOYMENT_PCT = float(os.getenv("MAX_STOCK_DEPLOYMENT_PCT", "0.95"))

    TA_RSI_OVERSOLD = float(os.getenv("TA_RSI_OVERSOLD", "35"))
    TA_RSI_OVERBOUGHT = float(os.getenv("TA_RSI_OVERBOUGHT", "65"))
    TA_RSI_WEIGHT = float(os.getenv("TA_RSI_WEIGHT", "1.0"))
    TA_MACD_WEIGHT = float(os.getenv("TA_MACD_WEIGHT", "1.0"))
    TA_BB_WEIGHT = float(os.getenv("TA_BB_WEIGHT", "1.0"))
    TA_BB_LOWER_THRESHOLD = float(os.getenv("TA_BB_LOWER_THRESHOLD", "0.10"))
    TA_BB_UPPER_THRESHOLD = float(os.getenv("TA_BB_UPPER_THRESHOLD", "0.90"))
    TA_TREND_WEIGHT = float(os.getenv("TA_TREND_WEIGHT", "1.0"))
    TA_MOM_WEIGHT = float(os.getenv("TA_MOM_WEIGHT", "1.0"))
    TA_MOM_THRESHOLD = float(os.getenv("TA_MOM_THRESHOLD", "2.0"))
    TA_VOL_THRESHOLD = float(os.getenv("TA_VOL_THRESHOLD", "1.2"))
    TA_VOL_BOOST = float(os.getenv("TA_VOL_BOOST", "1.2"))
    TA_MIN_BUY_SCORE = float(os.getenv("TA_MIN_BUY_SCORE", "1.0"))
    TA_MIN_SELL_SCORE = float(os.getenv("TA_MIN_SELL_SCORE", "1.0"))
    # Entry over-extension guards. The momentum buy_score has no upside ceiling,
    # so a name already up double-digits, sitting above its upper Bollinger band,
    # at a high RSI still scores full marks — i.e. the bot chases tops, then risk
    # exits cut them at small losses (the 6W/15L pattern, week of 2026-06-09).
    # Each guard below zeroes the BUY score (sell signals untouched) when a name
    # is too extended to open a new long. Set RSI/BB high or DAY_GAIN large to
    # disable an individual guard. See score_signals() and PARAMETERS.md.
    TA_RSI_BUY_MAX = float(os.getenv("TA_RSI_BUY_MAX", "72"))        # no new longs at/above this RSI (was a flat 80 hard cutoff)
    TA_BB_BUY_MAX = float(os.getenv("TA_BB_BUY_MAX", "1.0"))         # no buys at/above the upper Bollinger band (bb position)
    TA_MAX_DAY_GAIN_PCT = float(os.getenv("TA_MAX_DAY_GAIN_PCT", "10.0"))  # no chasing a name already up this % on the day
    # Cost control: skip the LLM scoring call on "idle" cycles — cycles where no
    # buy candidate clears the buy bar AND no held position needs a sell review.
    # On those cycles the LLM only ever returns HOLD, so skipping is behaviour-
    # neutral: deterministic stop-losses and holding-period exits run every cycle,
    # earlier in run_cycle and independent of this gate. See needs_llm_review() in
    # llm_engine. Set to false to force an LLM call every cycle.
    LLM_SKIP_IDLE_CYCLES = os.getenv("LLM_SKIP_IDLE_CYCLES", "true").lower() == "true"
    # P&L% at/below which a held position is "approaching its stop" and warrants
    # an LLM sell review even on an otherwise-idle cycle. Mirrors the discretionary
    # sell rule stated in the LLM prompt — both read this value, so they stay in sync.
    LLM_SELL_REVIEW_PNL_PCT = float(os.getenv("LLM_SELL_REVIEW_PNL_PCT", "-2.5"))
    # TA_STOP_LOSS_PCT is now an alias of RISK_STOP_LOSS_PCT. Kept for code that
    # historically read Config.TA_STOP_LOSS_PCT (LLM prompt, panels, etc.). Always
    # mirrors RISK_STOP_LOSS_PCT so changing one place is enough.
    TA_STOP_LOSS_PCT = RISK_STOP_LOSS_PCT

    TIMEZONE = os.getenv("TIMEZONE", "UTC")
    NOTIFY_PROVIDER = os.getenv("NOTIFY_PROVIDER", "whatsapp")
    NOTIFY_WHATSAPP_API_KEY = os.getenv("NOTIFY_WHATSAPP_API_KEY", "")
    NOTIFY_WHATSAPP_PHONE = os.getenv("NOTIFY_WHATSAPP_PHONE", "")
    NOTIFY_NTFY_TOPIC = os.getenv("NOTIFY_NTFY_TOPIC", "trading-agent")

    EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "false").lower() == "true"
    EMAIL_SMTP_HOST = os.getenv("EMAIL_SMTP_HOST", "smtp-mail.outlook.com")
    EMAIL_SMTP_PORT = int(os.getenv("EMAIL_SMTP_PORT", "587"))
    EMAIL_USER = os.getenv("EMAIL_USER", "")
    EMAIL_PASS = os.getenv("EMAIL_PASS", "")
    EMAIL_TO = os.getenv("EMAIL_TO", "")
    EMAIL_FROM = os.getenv("EMAIL_FROM", "")
    EMAIL_API_KEY = os.getenv("EMAIL_API_KEY", "")

    @staticmethod
    def target_positions(trading_capital: float) -> int:
        """Live book size: TARGET_POSITIONS at TARGET_POSITIONS_REF_CAPITAL, growing
        ~logarithmically with trading capital (+TARGET_POSITIONS_PER_10X names per 10×),
        clamped to [TARGET_POSITIONS_MIN, TARGET_POSITIONS_MAX]. Below the reference
        capital it floors at the base. Drives per-position size and the daily trade cap."""
        ref = Config.TARGET_POSITIONS_REF_CAPITAL
        n = Config.TARGET_POSITIONS + Config.TARGET_POSITIONS_PER_10X * math.log10(max(trading_capital, ref) / ref)
        return int(max(Config.TARGET_POSITIONS_MIN, min(round(n), Config.TARGET_POSITIONS_MAX)))

    @staticmethod
    def max_position_dollars(trading_capital: float) -> float:
        """Per-position dollar cap: equal-weight slice (capital ÷ live book size),
        floored at POSITION_MIN_DOLLARS and capped at POSITION_HARD_CAP_PCT of capital.
        Replaces the hardcoded max(50, min(slice, 2000)) that broke at high equity."""
        if trading_capital <= 0:
            return Config.POSITION_MIN_DOLLARS
        per_trade = trading_capital / Config.target_positions(trading_capital)
        return max(Config.POSITION_MIN_DOLLARS, min(per_trade, trading_capital * Config.POSITION_HARD_CAP_PCT))

    @staticmethod
    def max_trades_per_day(trading_capital: float) -> int:
        """Daily voluntary-trade cap. An explicit RISK_MAX_TRADES_PER_DAY env pin wins
        (manual flat cap); otherwise RISK_MAX_TRADES_MULT × the equity-scaled book size."""
        if Config._RISK_MAX_TRADES_OVERRIDE:
            return int(Config._RISK_MAX_TRADES_OVERRIDE)
        return Config.RISK_MAX_TRADES_MULT * Config.target_positions(trading_capital)

    @staticmethod
    def get_notification_config() -> dict:
        if Config.NOTIFY_PROVIDER == "whatsapp":
            return {
                "api_key": Config.NOTIFY_WHATSAPP_API_KEY,
                "phone": Config.NOTIFY_WHATSAPP_PHONE
            }
        elif Config.NOTIFY_PROVIDER == "ntfy":
            return {
                "topic": Config.NOTIFY_NTFY_TOPIC
            }
        elif Config.NOTIFY_PROVIDER == "telegram":
            return {
                "bot_token": os.getenv("NOTIFY_TELEGRAM_BOT_TOKEN", ""),
                "chat_id": os.getenv("NOTIFY_TELEGRAM_CHAT_ID", "")
            }
        return {}

    @staticmethod
    def validate():
        if not Config.ALPACA_API_KEY or Config.ALPACA_API_KEY == "your_key_here":
            raise ValueError("Set ALPACA_API_KEY in .env")
        if not Config.ALPACA_SECRET_KEY or Config.ALPACA_SECRET_KEY == "your_secret_here":
            raise ValueError("Set ALPACA_SECRET_KEY in .env")
        if Config.LLM_PROVIDER == "anthropic":
            if not Config.ANTHROPIC_API_KEY or Config.ANTHROPIC_API_KEY == "your_anthropic_key_here":
                raise ValueError("Set ANTHROPIC_API_KEY in .env")
        else:
            if not Config.LLM_API_KEY or Config.LLM_API_KEY == "your_openrouter_key_here":
                raise ValueError("Set LLM_API_KEY in .env")
