import logging
import requests
from datetime import datetime
import pytz

logger = logging.getLogger("trader.notifications")

def _local_now():
    from .config import Config
    tz = pytz.timezone(Config.TIMEZONE)
    return datetime.now(pytz.UTC).astimezone(tz)

class NotificationManager:
    def __init__(self, provider: str = "whatsapp", config: dict = None):
        self.provider = provider
        self.config = config or {}
        self.enabled = True
        if self.provider == "whatsapp":
            self.enabled = bool(self.config.get("api_key"))
        elif self.provider == "ntfy":
            self.enabled = bool(self.config.get("topic"))
        elif self.provider == "telegram":
            self.enabled = bool(self.config.get("bot_token") and self.config.get("chat_id"))

    def send(self, message: str, priority: str = "normal"):
        if not self.enabled:
            logger.info(f"Notification disabled. Would send: {message[:50]}...")
            return False

        try:
            if self.provider == "whatsapp":
                return self._send_whatsapp(message)
            elif self.provider == "ntfy":
                return self._send_ntfy(message, priority)
            elif self.provider == "telegram":
                return self._send_telegram(message)
        except Exception as e:
            logger.error(f"Notification failed: {e}")
            return False

    def _send_whatsapp(self, message: str) -> bool:
        api_key = self.config.get("api_key")
        phone = self.config.get("phone", "")

        url = "https://api.callmebot.com/whatsapp.php"
        params = {
            "phone": phone,
            "apikey": api_key,
            "text": message
        }

        resp = requests.get(url, params=params, timeout=10)
        if "Message queued" in resp.text:
            logger.info(f"WhatsApp notification sent")
            return True
        else:
            logger.warning(f"WhatsApp API response: {resp.text[:100]}")
            return False

    def _send_ntfy(self, message: str, priority: str = "normal") -> bool:
        topic = self.config.get("topic", "trading-agent")

        priority_map = {"low": 1, "normal": 3, "high": 7, "urgent": 9}
        url = f"https://ntfy.sh/{topic}"

        resp = requests.post(url,
            data=message.encode('utf-8'),
            headers={
                "Priority": str(priority_map.get(priority, 3)),
                "Title": "Trading Alert",
                "Tags": "chart_with_upwards_trend"
            },
            timeout=10
        )
        return resp.status_code == 200

    def _send_telegram(self, message: str) -> bool:
        bot_token = self.config.get("bot_token")
        chat_id = self.config.get("chat_id")

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }

        resp = requests.post(url, json=data, timeout=10)
        return resp.status_code == 200

    def notify_trade(self, action: str, symbol: str, quantity: float, price: float, pnl: float = None):
        msg = (f"TRADE ALERT\n"
               f"Action: {action}\n"
               f"Stock: {symbol}\n"
               f"Qty: {quantity}\n"
               f"Price: ${price:.2f}")
        if pnl is not None:
            msg += f"\nP&L: {pnl:+.2f}%"

        self.send(msg)

    def notify_daily_summary(self, stats: dict):
        msg = (f"DAILY SUMMARY\n"
               f"Trades: {stats.get('trades', 0)}\n"
               f"P&L: ${stats.get('pnl', 0):.2f}\n"
               f"Portfolio: ${stats.get('portfolio', 0):.2f}\n"
               f"Cash: ${stats.get('cash', 0):.2f}")
        self.send(msg, priority="low")

    def notify_alert(self, alert_type: str, message: str):
        msg = f"ALERT [{alert_type}]\n{message}"
        self.send(msg, priority="high")

    def notify_stop_loss(self, symbol: str, entry: float, exit: float, pnl: float):
        msg = (f"STOP LOSS TRIGGERED\n"
               f"Stock: {symbol}\n"
               f"Entry: ${entry:.2f}\n"
               f"Exit: ${exit:.2f}\n"
               f"Loss: {pnl:.2f}%")
        self.send(msg, priority="high")

    def notify_market_open(self):
        self.send("MARKET OPEN - Trading bot is now active.")

    def notify_market_close(self):
        self.send("MARKET CLOSED - Trading bot is idle until next session.")

    def notify_daily_loss_limit(self, pnl: float, limit: float):
        msg = f"DAILY LOSS LIMIT HIT\nP&L: ${pnl:.2f} / Limit: ${limit:.2f}\nTrading stopped for today."
        self.send(msg, priority="high")

    def notify_bot_startup(self, strategy_info: str = None):
        from .config import Config
        stop_pct = abs(Config.TA_STOP_LOSS_PCT) * 100

        if not strategy_info:
            strategy_info = f"Active (RSI {Config.TA_RSI_OVERSOLD}/{Config.TA_RSI_OVERBOUGHT})"

        msg = (f"BOT STARTUP\n"
               f"Time: {_local_now().strftime('%I:%M %p %Z')}\n"
               f"Strategy: {strategy_info}\n"
               f"Watchlist: 100 stocks (dynamic)\n"
               f"Stop Loss: {stop_pct:.0f}% | Max Position: {Config.RISK_MAX_POSITION_PCT:.0%}\n"
               f"Status: Ready for market open")
        self.send(msg)

    def notify_status_update(self, portfolio_value: float, cash: float, daily_pl: float, trades_today: int, max_trades: int):
        msg = (f"STATUS UPDATE\n"
               f"Time: {_local_now().strftime('%I:%M %p %Z')}\n"
               f"Portfolio: ${portfolio_value:.2f}\n"
               f"Cash: ${cash:.2f}\n"
               f"P&L: ${daily_pl:+.2f}\n"
               f"Trades: {trades_today}/{max_trades}")
        self.send(msg, priority="low")

    def test(self) -> bool:
        return self.send(f"Trading bot test message\nTime: {_local_now().strftime('%H:%M %Z')}\nStatus: OK")
