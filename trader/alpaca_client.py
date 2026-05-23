import logging
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, GetAssetsRequest, StopLossRequest, TakeProfitRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetStatus, OrderClass
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestBarRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from datetime import datetime, timedelta

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
        order = self.trading.close_position(symbol)
        logger.info(f"Position closed: {symbol}")
        return order

    def get_bars(self, symbol: str, days: int = 14):
        start = datetime.now() - timedelta(days=days)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
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

    def get_daily_pl(self):
        acct = self.get_account()
        return float(acct.equity) - float(acct.last_equity)
