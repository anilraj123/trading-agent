import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
    ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
    ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic")
    LLM_API_KEY = os.getenv("LLM_API_KEY")
    LLM_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

    RISK_MAX_POSITION_PCT = float(os.getenv("RISK_MAX_POSITION_PCT", "0.10"))
    RISK_DAILY_LOSS_LIMIT = float(os.getenv("RISK_DAILY_LOSS_LIMIT", "-5.00"))
    # Stop-loss fraction (negative). Falls back to legacy TA_STOP_LOSS_PCT for
    # backward compatibility with existing .env files; RISK_STOP_LOSS_PCT wins if set.
    RISK_STOP_LOSS_PCT = float(os.getenv("RISK_STOP_LOSS_PCT", os.getenv("TA_STOP_LOSS_PCT", "-0.03")))
    RISK_MAX_TRADES_PER_DAY = int(os.getenv("RISK_MAX_TRADES_PER_DAY", "5"))
    RISK_MAX_HOLDING_DAYS = int(os.getenv("RISK_MAX_HOLDING_DAYS", "3"))
    RISK_MIN_CONFIDENCE = float(os.getenv("RISK_MIN_CONFIDENCE", "0.6"))

    TRADING_INTERVAL_MINUTES = int(os.getenv("TRADING_INTERVAL_MINUTES", "15"))
    WATCHLIST = [s.strip() for s in os.getenv("WATCHLIST", "AAPL,MSFT,TSLA,SPY,QQQ").split(",")]
    BLACKLIST = [s.strip().upper() for s in os.getenv("BLACKLIST", "").split(",") if s.strip()]
    SIMULATED_ACCOUNT_SIZE = float(os.getenv("SIMULATED_ACCOUNT_SIZE", "200"))

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
    TA_MIN_BUY_SCORE = float(os.getenv("TA_MIN_BUY_SCORE", "0.5"))
    TA_MIN_SELL_SCORE = float(os.getenv("TA_MIN_SELL_SCORE", "1.0"))
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
