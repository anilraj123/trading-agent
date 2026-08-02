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

def in_open_window(session_open_time, now, window_minutes: int) -> bool:
    """True when `now` is within window_minutes of `session_open_time` — the
    timestamp the bot recorded at this session's closed->open transition. Unlike
    in_close_window this can't be derived from Alpaca's clock alone (next_open
    refers to the *next* session once the market is already open), so the caller
    tracks the session's own open time and passes it in.
    Fails closed (False) when session_open_time is unknown: the disaster stop
    then evaluates normally, same as before this gate existed."""
    if session_open_time is None or now is None:
        return False
    try:
        return (now - session_open_time).total_seconds() <= window_minutes * 60
    except TypeError:
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
        # Per-symbol trailing state: {"hwm": float, "trailing": bool}. Persisted
        # (unlike self.positions) — a restart must not reset a winner's high-water
        # mark, or its trailing exit silently moves down. "trailing" is a sticky
        # explicit bool, NOT derived from hwm vs entry: entry is rebuilt from
        # Alpaca's avg_entry_price after a restart and drifts on top-ups, so
        # deriving could de-activate a trailing winner and re-expose it to expiry.
        self.position_trail: dict[str, dict] = {}
        # US market holidays (weekday non-sessions) used to count the holding period
        # in *trading* days rather than calendar days. Refreshed daily by the bot from
        # Alpaca's calendar; empty list ⇒ weekends-only (still excludes Sat/Sun).
        self.market_holidays: list[date] = []
        # Symbols that hit a stop (disaster/close/trailing) TODAY. Blocks rebuying
        # for the rest of the session — a stop-loss firing and then the LLM
        # re-entering the same name hours later on the same signal defeats the
        # stop (live case: MO disaster-stopped 2026-07-30 9:42am, rebought 3x that
        # afternoon). Reset daily alongside the other daily counters.
        self.stopped_out_today: set[str] = set()
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
                "position_trail": self.position_trail,
                "stopped_out_today": sorted(self.stopped_out_today),
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
            # Missing key (pre-trailing state file) loads cleanly as {}.
            self.position_trail = state.get("position_trail", {})
            # Missing key (pre-cooldown state file) loads cleanly as set().
            self.stopped_out_today = set(state.get("stopped_out_today", []))
            logger.info(f"Loaded risk state: {self.daily_trades} trades, ${self.daily_pnl:.2f} P&L, {len(self.position_entry_dates)} entry dates")
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            pass

    def reset_if_new_day(self):
        if date.today() != self.last_reset_date:
            logger.info("New trading day - resetting daily counters")
            self.daily_trades = 0
            self.daily_pnl = 0.0
            self.trade_log = []
            self.stopped_out_today = set()
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
        # Trail entry: a top-up of an already-trailing winner must NOT reset its
        # high-water mark or de-activate the trail — merge, don't overwrite.
        existing = self.position_trail.get(symbol)
        if existing:
            existing["hwm"] = max(existing["hwm"], entry_price)
        else:
            self.position_trail[symbol] = {"hwm": entry_price, "trailing": False}
        self._save_state()

    def unregister_position(self, symbol: str):
        """Call AFTER a sell/close order is successfully submitted."""
        self.positions.pop(symbol, None)
        self.position_entry_dates.pop(symbol, None)
        self.position_trail.pop(symbol, None)
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

    def check_stop_losses(self, current_positions: list, close_window: bool = False, open_window: bool = False) -> list:
        """Two-tier stops. The disaster stop (RISK_INTRADAY_STOP_PCT) fires on
        every call EXCEPT during the opening window (open_window=True) — a
        position that gapped down overnight can print its most extreme price in
        the first minute or two of the next session on thin opening-auction
        liquidity, and that gap is exactly the kind of intraday noise the
        close-window deferral already treats specially for the regular stop. The
        regular stop (RISK_STOP_LOSS_PCT) fires only when close_window=True — the
        TA signal is daily-bar, so realizing the -3% stop on intraday prints sold
        noise, not signal (0-for-11 live). At most one trigger per symbol;
        disaster takes precedence."""
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

            if current_price <= disaster_price and not open_window:
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

    def update_trailing(self, current_positions: list):
        """Ratchet each tracked position's high-water mark (up only) and flip it
        to 'trailing' once the hwm reaches entry x (1 + RISK_TRAIL_ACTIVATE_PCT).
        Sticky: once trailing, always trailing (until the position closes).
        Call once per cycle BEFORE any exit checks."""
        changed = False
        for pos in current_positions:
            sym = pos.symbol
            if sym not in self.positions:
                continue
            price = self._position_price(pos)
            if price <= 0:
                continue
            t = self.position_trail.setdefault(sym, {"hwm": self.positions[sym]["entry_price"], "trailing": False})
            if price > t["hwm"]:
                t["hwm"] = price
                changed = True
            entry = self.positions[sym]["entry_price"]
            if not t["trailing"] and entry > 0 and t["hwm"] >= entry * (1 + Config.RISK_TRAIL_ACTIVATE_PCT):
                t["trailing"] = True
                changed = True
                logger.info(f"Trailing activated for {sym}: hwm=${t['hwm']:.2f} (entry ${entry:.2f}) — exempt from {Config.RISK_MAX_HOLDING_DAYS}-day expiry")
        if changed:
            self._save_state()

    def check_trailing_stops(self, current_positions: list) -> list:
        """Trailing exits for activated winners: fire when price <= hwm x
        (1 + RISK_TRAIL_STOP_PCT). Evaluated every cycle — unlike the close-window
        stop this realizes a locked gain (floor ~ breakeven by construction), and
        close-only evaluation would expose an activated winner to a full intraday
        reversal. Skips symbols already removed from self.positions this cycle."""
        triggers = []
        for pos in current_positions:
            sym = pos.symbol
            if sym not in self.positions:
                continue
            t = self.position_trail.get(sym)
            if not t or not t.get("trailing"):
                continue
            price = self._position_price(pos)
            trail_price = t["hwm"] * (1 + Config.RISK_TRAIL_STOP_PCT)
            if 0 < price <= trail_price:
                triggers.append({
                    "symbol": sym,
                    "hwm": t["hwm"],
                    "trail_price": trail_price,
                    "current_price": price,
                    "quantity": float(pos.qty),
                    "entry_price": self.positions[sym]["entry_price"],
                    "stop_type": "trailing",
                })
                logger.warning(f"TRAILING STOP TRIGGERED: {sym} at ${price:.2f} (hwm ${t['hwm']:.2f} → trail ${trail_price:.2f})")
        return triggers

    def is_trailing(self, symbol: str) -> bool:
        return self.position_trail.get(symbol, {}).get("trailing", False)

    def mark_stopped_out(self, symbol: str):
        """Call after a stop (disaster, close-window, or trailing) closes a
        position. Blocks the LLM from rebuying `symbol` for the rest of the
        trading day — see stopped_out_today docstring in __init__."""
        self.reset_if_new_day()
        self.stopped_out_today.add(symbol)
        self._save_state()

    def is_cooling_down(self, symbol: str) -> bool:
        self.reset_if_new_day()
        return symbol in self.stopped_out_today

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
            # Trailing winners are exempt: their exit is the hwm-based trailing
            # stop (floor ~ breakeven), not the clock — the expiry was truncating
            # exactly the winners the strategy needs to pay for its losers.
            if self.is_trailing(pos.symbol):
                continue
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
                    # NEVER clobber a persisted trail entry — that's the whole
                    # point of persisting it (a restart must not reset a winner's
                    # hwm and silently lower its trailing exit). Only initialize
                    # symbols we've never tracked: hwm = max(entry, current) — we
                    # know the price reached at least the current level; entry
                    # alone would register a fake drawdown for a synced winner,
                    # current alone would under-set hwm for one that just dipped.
                    if symbol not in self.position_trail:
                        current = self._position_price(pos)
                        hwm = max(entry_price, current) if current > 0 else entry_price
                        self.position_trail[symbol] = {
                            "hwm": hwm,
                            "trailing": hwm >= entry_price * (1 + Config.RISK_TRAIL_ACTIVATE_PCT),
                        }
                    logger.info(f"Synced {symbol} from Alpaca: entry=${entry_price:.2f} stop=${stop_loss:.2f}")
                except (ValueError, AttributeError) as e:
                    logger.debug(f"Could not sync {symbol}: {e}")

        for symbol in list(self.positions.keys()):
            if symbol not in alpaca_symbols:
                self.positions.pop(symbol, None)
                self.position_entry_dates.pop(symbol, None)
        # Trail entries are persisted and can reference symbols self.positions
        # doesn't know (restart: trail state loaded from disk, positions rebuilt
        # here) — sweep them against Alpaca directly or stale winners' state
        # leaks forever.
        for symbol in list(self.position_trail.keys()):
            if symbol not in alpaca_symbols:
                self.position_trail.pop(symbol, None)
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
            "trailing_positions": sum(1 for s in self.positions if self.is_trailing(s)),
            "stop_loss_pct": Config.RISK_STOP_LOSS_PCT,
            "intraday_stop_pct": Config.RISK_INTRADAY_STOP_PCT,
            "close_window_min": Config.RISK_CLOSE_WINDOW_MIN,
            "cooldown_symbols": sorted(self.stopped_out_today),
        }
