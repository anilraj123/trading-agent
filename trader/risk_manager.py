import logging
from datetime import datetime, date
from .config import Config

logger = logging.getLogger("trader.risk")

class RiskManager:
    def __init__(self):
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.total_realized_pnl = 0.0
        self.trade_log = []
        self.last_reset_date = date.today()
        self.positions = {}
        self.position_entry_dates: dict[str, datetime] = {}

    def reset_if_new_day(self):
        if date.today() != self.last_reset_date:
            logger.info("New trading day - resetting daily counters")
            self.daily_trades = 0
            self.daily_pnl = 0.0
            self.trade_log = []
            self.last_reset_date = date.today()

    def can_trade(self, portfolio_value: float) -> tuple[bool, str]:
        self.reset_if_new_day()

        if self.daily_trades >= Config.RISK_MAX_TRADES_PER_DAY:
            return False, f"Daily trade limit reached ({self.daily_trades}/{Config.RISK_MAX_TRADES_PER_DAY})"

        daily_loss_limit = Config.RISK_DAILY_LOSS_LIMIT / 100 * portfolio_value
        if self.daily_pnl <= daily_loss_limit:
            return False, f"Daily loss limit hit (${self.daily_pnl:.2f} / ${daily_loss_limit:.2f})"

        return True, "OK"

    def validate_order(self, decision: dict, portfolio_value: float, cash: float, current_positions: list) -> tuple[bool, str]:
        symbol = decision.get("symbol")
        action = decision.get("action")
        quantity = decision.get("quantity", 0)
        confidence = decision.get("confidence", 0)
        current_price = decision.get("current_price", 0)

        if confidence < Config.RISK_MIN_CONFIDENCE:
            return False, f"Confidence too low ({confidence:.2f} < {Config.RISK_MIN_CONFIDENCE})"

        # validate_order is now PURE validation. It does NOT mutate self.positions —
        # registering a position before the broker accepts the order produced phantom
        # entries when submission failed, which then triggered software stop-loss checks
        # against positions that didn't exist. The caller is responsible for invoking
        # register_position / unregister_position only after a successful Alpaca submit.
        if action == "BUY":
            if quantity <= 0:
                return False, "Invalid buy quantity"

            position_value = quantity * current_price
            max_position = portfolio_value * Config.RISK_MAX_POSITION_PCT
            if position_value > max_position:
                max_shares = max_position / current_price
                return False, f"Position too large. Max ${max_position:.0f}, need ${position_value:.2f}. Max shares: {max_shares:.2f}"

            if position_value > cash * 0.95:
                return False, f"Not enough cash. Need ${position_value:.2f}, have ${cash:.2f}"

        elif action == "SELL":
            own_any = any(p.symbol == symbol for p in current_positions)
            if not own_any:
                return False, f"No position in {symbol} to sell"

        return True, "Approved"

    def register_position(self, symbol: str, entry_price: float, quantity: float):
        """Call AFTER a buy order is successfully submitted. Records the entry price,
        software stop-loss level, and entry timestamp used by check_stop_losses /
        get_expired_positions."""
        stop_loss = entry_price * (1 + Config.TA_STOP_LOSS_PCT)
        self.positions[symbol] = {
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "quantity": quantity,
            "date": datetime.now()
        }
        logger.info(f"Stop loss set for {symbol}: ${stop_loss:.2f} ({Config.TA_STOP_LOSS_PCT:.0%} from ${entry_price:.2f})")

    def unregister_position(self, symbol: str):
        """Call AFTER a sell/close order is successfully submitted."""
        self.positions.pop(symbol, None)
        self.position_entry_dates.pop(symbol, None)

    def check_stop_losses(self, current_positions: list) -> list:
        stop_loss_triggers = []
        for pos in current_positions:
            symbol = pos.symbol
            if symbol in self.positions:
                stop_price = self.positions[symbol]["stop_loss"]
                current_price = float(pos.last_price) if hasattr(pos, 'last_price') else float(pos.market_value) / float(pos.qty) if float(pos.qty) > 0 else 0

                if current_price <= stop_price and current_price > 0:
                    stop_loss_triggers.append({
                        "symbol": symbol,
                        "stop_price": stop_price,
                        "current_price": current_price,
                        "quantity": float(pos.qty),
                        "entry_price": self.positions[symbol]["entry_price"]
                    })
                    logger.warning(f"STOP LOSS TRIGGERED: {symbol} at ${current_price:.2f} (stop: ${stop_price:.2f})")

        return stop_loss_triggers

    def record_trade(self, symbol: str, action: str, quantity: float, price: float, pnl: float = 0, pnl_dollars: float = 0, strategy: str = "unknown", counts_toward_daily_cap: bool = True):
        # `pnl` is the percentage gain/loss on this trade (used for the wins/losses
        # buckets and the human-readable log). `pnl_dollars` is what feeds the
        # daily loss-limit check, because Config.RISK_DAILY_LOSS_LIMIT is expressed
        # as a percent of trading capital and gets converted to dollars in can_trade.
        # Mixing the two units silently disabled the daily loss limit before this fix.
        if counts_toward_daily_cap:
            self.daily_trades += 1
        self.daily_pnl += pnl_dollars
        self.trade_log.append({
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "action": action,
            "quantity": quantity,
            "price": price,
            "pnl": pnl,
            "pnl_dollars": pnl_dollars,
            "strategy": strategy
        })
        if "BUY" in action:
            self.position_entry_dates[symbol] = datetime.now()
        elif "SELL" in action:
            self.position_entry_dates.pop(symbol, None)
        logger.info(f"Trade recorded: {action} {quantity} {symbol} @ ${price:.2f} | PnL: {pnl:+.2f}% (${pnl_dollars:+.2f})")

    def get_expired_positions(self, current_positions: list, max_days: int = None) -> list[str]:
        if max_days is None:
            max_days = Config.RISK_MAX_HOLDING_DAYS
        expired = []
        now = datetime.now()
        for pos in current_positions:
            entry = self.position_entry_dates.get(pos.symbol)
            if entry and (now - entry).days >= max_days:
                expired.append(pos.symbol)
        if expired:
            logger.info(f"Expired positions ({max_days}+ days): {', '.join(expired)}")
        return expired

    def get_stop_loss_price(self, entry_price: float) -> float:
        return round(entry_price * (1 + Config.TA_STOP_LOSS_PCT), 2)

    def sync_from_alpaca(self, positions: list):
        """Populate risk-manager state from Alpaca positions for symbols not
        already tracked. Gives stop-loss, expiry, and PDT coverage to positions
        opened manually or before bot restart. Bot-registered entries are
        preserved (they have accurate entry timestamps)."""
        now = datetime.now()
        alpaca_symbols = set()
        for pos in positions:
            symbol = pos.symbol
            alpaca_symbols.add(symbol)
            if symbol not in self.positions:
                try:
                    entry_price = float(pos.avg_entry_price)
                    qty = float(pos.qty)
                    stop_loss = entry_price * (1 + Config.TA_STOP_LOSS_PCT)
                    self.positions[symbol] = {
                        "entry_price": entry_price,
                        "stop_loss": stop_loss,
                        "quantity": qty,
                        "date": now
                    }
                    self.position_entry_dates[symbol] = now
                    logger.info(f"Synced {symbol} from Alpaca: entry=${entry_price:.2f} stop=${stop_loss:.2f}")
                except (ValueError, AttributeError) as e:
                    logger.debug(f"Could not sync {symbol}: {e}")

        for symbol in list(self.positions.keys()):
            if symbol not in alpaca_symbols:
                self.positions.pop(symbol, None)
                self.position_entry_dates.pop(symbol, None)

    def get_status(self, portfolio_value: float = None) -> dict:
        pv = portfolio_value if portfolio_value else Config.SIMULATED_ACCOUNT_SIZE
        daily_loss_limit = Config.RISK_DAILY_LOSS_LIMIT / 100 * pv
        return {
            "daily_trades": self.daily_trades,
            "daily_pnl": self.daily_pnl,
            "max_trades": Config.RISK_MAX_TRADES_PER_DAY,
            "daily_loss_limit": round(daily_loss_limit, 2),
            "remaining_capacity": Config.RISK_MAX_TRADES_PER_DAY - self.daily_trades,
            "distance_to_loss_limit": round(self.daily_pnl - daily_loss_limit, 2),
            "open_positions": len(self.positions),
            "stop_loss_pct": Config.TA_STOP_LOSS_PCT
        }
