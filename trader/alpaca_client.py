import logging
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, GetAssetsRequest, StopLossRequest, TakeProfitRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetStatus, OrderClass
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestBarRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from datetime import datetime, timedelta, date

from .config import Config

logger = logging.getLogger("trader.alpaca")

class AlpacaClient:
    def __init__(self):
        is_paper = "paper-api" in Config.ALPACA_BASE_URL
        self.trading = TradingClient(Config.ALPACA_API_KEY, Config.ALPACA_SECRET_KEY, paper=is_paper)
        self.data = StockHistoricalDataClient(Config.ALPACA_API_KEY, Config.ALPACA_SECRET_KEY)

    def get_account(self):
        return self.trading.get_account()

    def get_positions(self):
        return self.trading.get_all_positions()

    def get_portfolio_value(self):
        acct = self.get_account()
        return float(acct.equity)

    def get_cash(self):
        acct = self.get_account()
        return float(acct.cash)

    def get_orders(self):
        return self.trading.get_orders()

    def get_open_orders(self):
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        return self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))

    def cancel_all_orders(self):
        self.trading.cancel_orders()

    def cancel_orders_for_symbol(self, symbol: str) -> int:
        """Cancel all open orders for one symbol. Call before any bot-initiated
        close/sell: a resting bracket/OTO stop leg holds the position's qty and
        makes close_position / a SELL market order reject with 'insufficient qty'."""
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        try:
            orders = self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]))
        except Exception as e:
            logger.warning(f"Listing open orders for {symbol} failed: {e}")
            return 0
        for o in orders:
            try:
                self.trading.cancel_order_by_id(o.id)
                logger.info(f"Cancelled resting order {o.id} for {symbol}")
            except Exception as e:
                logger.warning(f"Cancel {o.id} for {symbol} failed: {e}")
        return len(orders)

    def submit_market_order(self, symbol: str, side: OrderSide, qty: float, stop_loss: float = None, take_profit: float = None):
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY
        )

        if stop_loss and take_profit:
            req.order_class = OrderClass.BRACKET
            req.stop_loss = StopLossRequest(stop_price=stop_loss)
            req.take_profit = TakeProfitRequest(limit_price=take_profit)
        elif stop_loss:
            req.order_class = OrderClass.OTO
            req.stop_loss = StopLossRequest(stop_price=stop_loss)
        elif take_profit:
            req.order_class = OrderClass.OTO
            req.take_profit = TakeProfitRequest(limit_price=take_profit)

        order = self.trading.submit_order(req)
        logger.info(f"Order submitted: {side} {qty} {symbol} (id={order.id})")
        return order

    def submit_limit_order(self, symbol: str, side: OrderSide, qty: float, limit_price: float):
        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            limit_price=limit_price,
            side=side,
            time_in_force=TimeInForce.DAY
        )
        order = self.trading.submit_order(req)
        logger.info(f"Limit order submitted: {side} {qty} {symbol} @ ${limit_price}")
        return order

    def close_position(self, symbol: str):
        # Every close_position in this bot is a full liquidation, so cancelling the
        # symbol's resting orders first is always correct — a live bracket/OTO stop
        # leg would otherwise hold the qty and reject the close.
        self.cancel_orders_for_symbol(symbol)
        order = self.trading.close_position(symbol)
        logger.info(f"Position closed: {symbol}")
        return order

    def get_bars(self, symbol: str, days: int = 14, timeframe: TimeFrame = None):
        if timeframe is None:
            timeframe = TimeFrame(5, TimeFrameUnit.Minute)
        start = datetime.now() - timedelta(days=days)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=timeframe,
            start=start
        )
        bars = self.data.get_stock_bars(req)
        return bars.df if len(bars.df) > 0 else None

    def get_bars_batch(self, symbols: list, days: int = 14, timeframe: TimeFrame = None):
        if not symbols:
            return None
        if timeframe is None:
            timeframe = TimeFrame(5, TimeFrameUnit.Minute)
        start = datetime.now() - timedelta(days=days)
        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=timeframe,
            start=start
        )
        bars = self.data.get_stock_bars(req)
        return bars.df if len(bars.df) > 0 else None

    def get_latest_price(self, symbol: str):
        req = StockLatestBarRequest(symbol_or_symbols=symbol)
        bar = self.data.get_stock_latest_bar(req)
        return bar[symbol].close if symbol in bar else None

    def get_market_status(self):
        clock = self.trading.get_clock()
        return clock.is_open

    def get_clock(self):
        """Full Alpaca clock: is_open, timestamp, next_open, next_close — all
        tz-aware and from the same response, so time-to-close math is immune to
        local-clock drift, DST, and early-close (1pm ET) days."""
        return self.trading.get_clock()

    def get_market_holidays(self, start: date, end: date) -> list:
        """Weekday dates in [start, end] that are NOT trading sessions (US market
        holidays), from Alpaca's official calendar. Feeds the trading-day holding-
        period clock. Returns [] on any failure → caller degrades to weekends-only."""
        from alpaca.trading.requests import GetCalendarRequest
        try:
            sessions = self.trading.get_calendar(GetCalendarRequest(start=start, end=end))
            session_dates = set()
            for s in sessions:
                sd = s.date if isinstance(s.date, date) else datetime.fromisoformat(str(s.date)[:10]).date()
                session_dates.add(sd)
        except Exception as e:
            logger.warning(f"get_market_holidays failed ({e}); using weekends-only")
            return []
        holidays, d = [], start
        while d <= end:
            if d.weekday() < 5 and d not in session_dates:
                holidays.append(d)
            d += timedelta(days=1)
        return holidays

    def get_daily_pl(self):
        acct = self.get_account()
        return float(acct.equity) - float(acct.last_equity)

    def get_cash_deposits(self) -> list:
        """All executed cash movements (deposits positive, withdrawals negative)
        as [(date, amount)], ascending. Authoritative source for the SPY
        buy-and-hold benchmark — replaces guessing the deposit schedule from
        equity jumps. Returns [] on any failure so callers can fall back."""
        import requests
        try:
            resp = requests.get(
                f"{Config.ALPACA_BASE_URL}/v2/account/activities",
                headers={
                    "APCA-API-KEY-ID": Config.ALPACA_API_KEY,
                    "APCA-API-SECRET-KEY": Config.ALPACA_SECRET_KEY,
                },
                params={"activity_types": "CSD,CSW,TRANS", "direction": "asc", "page_size": 100},
                timeout=10,
            )
            resp.raise_for_status()
            out = []
            for a in resp.json():
                if a.get("status") and a["status"] != "executed":
                    continue
                out.append((datetime.fromisoformat(a["date"]).date(), float(a["net_amount"])))
            return out
        except Exception as e:
            logger.warning(f"get_cash_deposits failed ({e}); benchmark will fall back")
            return []
