import json
import logging
import os
from datetime import datetime, date
import numpy as np
from .config import Config

logger = logging.getLogger("trader.risk")

STATE_FILE = f"{os.getenv('DATA_DIR', '/app/data')}/risk_state.json"

def in_close_window(clock, window_minutes: int) -> bool:
    """True when the session is open and within window_minutes of close.
    Compares clock.timestamp to clock.next_close from the SAME Alpaca clock
    response — immune to local-clock drift, DST, and early-close (1pm ET) days.
    Fails closed (False) on a malformed clock: the regular stop then defers to
    the next session's close window, which the live data says beats firing it
    intraday."""
    try:
        if not clock.is_open:
            return False
        return (clock.next_close - clock.timestamp).total_seconds() <= window_minutes * 60
    except (AttributeError, TypeError):
        return False

class RiskManager:
    def __init__(self):
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.total_realized_pnl = 0.0
        self.trade_log = []
        self.last_reset_date = date.today()
        self.positions = {}
        self.position_entry_dates: dict[str, datetime] = {}
        # US market holidays (weekday non-sessions) used to count the holding period
        # in *trading* days rather than calendar days. Refreshed daily by the bot from
        # Alpaca's calendar; empty list ⇒ weekends-only (still excludes Sat/Sun).
        self.market_holidays: list[date] = []
        self._load_state()

    def _state_path(self):
        return STATE_FILE

    def _save_state(self):
        path = self._state_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            state = {
                "daily_trades": self.daily_trades,
                "daily_pnl": self.daily_pnl,
                "total_realized_pnl": self.total_realized_pnl,
                "trade_log": self.trade_log,
                "last_reset_date": self.last_reset_date.isoformat(),
                "position_entry_dates": {
                    sym: dt.isoformat() for sym, dt in self.position_entry_dates.items()
                },
            }
            with open(path, "w") as f:
                json.dump(state, f)
        except Exception as e:
            logger.warning(f"Failed to save risk state: {e}")

    def _load_state(self):
        path = self._state_path()
        try:
            with open(path) as f:
                state = json.load(f)
            self.daily_trades = state.get("daily_trades", 0)
            self.daily_pnl = state.get("daily_pnl", 0.0)
            self.total_realized_pnl = state.get("total_realized_pnl", 0.0)
            self.trade_log = state.get("trade_log", [])
            reset_s = state.get("last_reset_date")
            if reset_s:
                self.last_reset_date = date.fromisoformat(reset_s)
            ped = state.get("position_entry_dates", {})
            self.position_entry_dates = {
                sym: datetime.fromisoformat(dt) for sym, dt in ped.items()
            }
            logger.info(f"Loaded risk state: {self.daily_trades} trades, ${self.daily_pnl:.2f} P&L, {len(self.position_entry_dates)} entry dates")
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            pass

    def reset_if_new_day(self):
        if date.today() != self.last_reset_date:
            logger.info("New trading day - resetting daily counters")
            self.daily_trades = 0
            self.daily_pnl = 0.0
            self.trade_log = []
            self.last_reset_date = date.today()
            self._save_state()

    def can_trade(self, portfolio_value: float, unrealized_pnl: float = 0.0) -> tuple[bool, str]:
        self.reset_if_new_day()

        max_trades = Config.max_trades_per_day(portfolio_value * Config.TRADING_CAPITAL_ALLOCATION)
        if self.daily_trades >= max_trades:
            return False, f"Daily trade limit reached ({self.daily_trades}/{max_trades})"

        total_daily_pnl = self.daily_pnl + unrealized_pnl
        daily_loss_limit = Config.RISK_DAILY_LOSS_LIMIT / 100 * portfolio_value
        if total_daily_pnl <= daily_loss_limit:
            return False, f"Daily loss limit hit (${total_daily_pnl:.2f} = realized ${self.daily_pnl:.2f} + unrealized ${unrealized_pnl:.2f}, limit ${daily_loss_limit:.2f})"

        return True, "OK"

    def validate_order(self, decision: dict, trading_capital: float, cash: float, current_positions: list) -> tuple[bool, str]:
        # `trading_capital` is the caller's ALREADY-ALLOCATED slice (account_value ×
        # TRADING_CAPITAL_ALLOCATION). Do NOT re-apply the allocation here — doing so
        # double-counted it and made max_position 0.6× tighter than the soft cap in
        # _execute_decisions, silently rejecting valid buys (e.g. a $56 position vs a
        # nominal $50 cap that should have been ~$73). This now uses the same anchor as
        # the caller's per_trade_size, so the two gates agree.
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
            max_position = Config.max_position_dollars(trading_capital)
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
        stop_loss = entry_price * (1 + Config.RISK_STOP_LOSS_PCT)
        disaster_stop = entry_price * (1 + Config.RISK_INTRADAY_STOP_PCT)
        self.positions[symbol] = {
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "disaster_stop": disaster_stop,
            "quantity": quantity,
            "date": datetime.now()
        }
        logger.info(f"Stops set for {symbol}: ${stop_loss:.2f} ({Config.RISK_STOP_LOSS_PCT:.0%} @close) / ${disaster_stop:.2f} ({Config.RISK_INTRADAY_STOP_PCT:.0%} intraday) from ${entry_price:.2f}")
        self._save_state()

    def unregister_position(self, symbol: str):
        """Call AFTER a sell/close order is successfully submitted."""
        self.positions.pop(symbol, None)
        self.position_entry_dates.pop(symbol, None)
        self._save_state()

    @staticmethod
    def _position_price(pos) -> float:
        """Best available price for an Alpaca position object. Live alpaca-py
        positions expose current_price (not last_price); tests use last_price
        via SimpleNamespace — keep it first for compat."""
        for attr in ("last_price", "current_price"):
            val = getattr(pos, attr, None)
            if val is not None:
                return float(val)
        qty = float(pos.qty)
        return float(pos.market_value) / qty if qty > 0 else 0.0

    def check_stop_losses(self, current_positions: list, close_window: bool = False) -> list:
        """Two-tier stops. The disaster stop (RISK_INTRADAY_STOP_PCT) fires on
        every call; the regular stop (RISK_STOP_LOSS_PCT) fires only when
        close_window=True — the TA signal is daily-bar, so realizing the -3%
        stop on intraday prints sold noise, not signal (0-for-11 live). At most
        one trigger per symbol; disaster takes precedence."""
        stop_loss_triggers = []
        for pos in current_positions:
            symbol = pos.symbol
            if symbol not in self.positions:
                continue
            entry = self.positions[symbol]["entry_price"]
            stop_price = self.positions[symbol]["stop_loss"]
            # Positions registered before the disaster tier existed: recompute.
            disaster_price = self.positions[symbol].get("disaster_stop", entry * (1 + Config.RISK_INTRADAY_STOP_PCT))
            current_price = self._position_price(pos)
            if current_price <= 0:
                continue

            if current_price <= disaster_price:
                stop_type = "intraday_disaster"
                triggered_at = disaster_price
            elif close_window and current_price <= stop_price:
                stop_type = "close_stop"
                triggered_at = stop_price
            else:
                continue

            stop_loss_triggers.append({
                "symbol": symbol,
                "stop_price": triggered_at,
                "current_price": current_price,
                "quantity": float(pos.qty),
                "entry_price": entry,
                "stop_type": stop_type,
            })
            logger.warning(f"STOP LOSS TRIGGERED [{stop_type}]: {symbol} at ${current_price:.2f} (stop: ${triggered_at:.2f})")

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
        self._save_state()

    @staticmethod
    def trading_days_between(start_date: date, end_date: date, holidays: list = None) -> int:
        """Number of trading sessions in [start_date, end_date) — weekends and the
        given market holidays excluded. Used for the holding-period clock so it
        counts *trading* days, not calendar days (a Fri buy isn't "3 days old" on
        Mon)."""
        hol = np.array(holidays or [], dtype="datetime64[D]")
        return int(np.busday_count(start_date, end_date, holidays=hol))

    def get_expired_positions(self, current_positions: list, max_days: int = None) -> list[str]:
        if max_days is None:
            max_days = Config.RISK_MAX_HOLDING_DAYS
        expired = []
        today = datetime.now().date()
        for pos in current_positions:
            entry = self.position_entry_dates.get(pos.symbol)
            if entry and self.trading_days_between(entry.date(), today, self.market_holidays) >= max_days:
                expired.append(pos.symbol)
        if expired:
            logger.info(f"Expired positions ({max_days}+ trading days): {', '.join(expired)}")
        return expired

    def get_stop_loss_price(self, entry_price: float) -> float:
        return round(entry_price * (1 + Config.RISK_STOP_LOSS_PCT), 2)

    def get_disaster_stop_price(self, entry_price: float) -> float:
        """Bracket-order stop-leg level: the broker-side leg is a redundant
        intraday backstop matching the software disaster stop, not the primary
        exit (that's the close-window stop)."""
        return round(entry_price * (1 + Config.RISK_INTRADAY_STOP_PCT), 2)

    def sync_from_alpaca(self, positions: list):
        """Populate risk-manager state from Alpaca positions for symbols not
        already tracked. Gives stop-loss and expiry coverage to positions
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
                    stop_loss = entry_price * (1 + Config.RISK_STOP_LOSS_PCT)
                    disaster_stop = entry_price * (1 + Config.RISK_INTRADAY_STOP_PCT)
                    self.positions[symbol] = {
                        "entry_price": entry_price,
                        "stop_loss": stop_loss,
                        "disaster_stop": disaster_stop,
                        "quantity": qty,
                        "date": now
                    }
                    # Don't overwrite persisted entry dates on restart
                    if symbol not in self.position_entry_dates:
                        self.position_entry_dates[symbol] = now
                    logger.info(f"Synced {symbol} from Alpaca: entry=${entry_price:.2f} stop=${stop_loss:.2f}")
                except (ValueError, AttributeError) as e:
                    logger.debug(f"Could not sync {symbol}: {e}")

        for symbol in list(self.positions.keys()):
            if symbol not in alpaca_symbols:
                self.positions.pop(symbol, None)
                self.position_entry_dates.pop(symbol, None)
        self._save_state()

    def get_status(self, portfolio_value: float = None) -> dict:
        pv = portfolio_value if portfolio_value else Config.SIMULATED_ACCOUNT_SIZE
        daily_loss_limit = Config.RISK_DAILY_LOSS_LIMIT / 100 * pv
        max_trades = Config.max_trades_per_day(pv * Config.TRADING_CAPITAL_ALLOCATION)
        return {
            "daily_trades": self.daily_trades,
            "daily_pnl": self.daily_pnl,
            "max_trades": max_trades,
            "daily_loss_limit": round(daily_loss_limit, 2),
            "remaining_capacity": max_trades - self.daily_trades,
            "distance_to_loss_limit": round(self.daily_pnl - daily_loss_limit, 2),
            "open_positions": len(self.positions),
            "stop_loss_pct": Config.RISK_STOP_LOSS_PCT,
            "intraday_stop_pct": Config.RISK_INTRADAY_STOP_PCT,
            "close_window_min": Config.RISK_CLOSE_WINDOW_MIN
        }
