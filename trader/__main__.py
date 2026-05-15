import logging
import time
import schedule
from datetime import datetime, timedelta
from alpaca.trading.enums import OrderSide
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel

from .config import Config
from .alpaca_client import AlpacaClient
from .llm_engine import LLMEngine
from .risk_manager import RiskManager
from .technical_analysis import TechnicalAnalysis
from .stock_discovery import StockDiscovery
from .notifications import NotificationManager
from .tracker import save_daily_snapshot, save_trade, generate_weekly_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(rich_tracebacks=True)]
)

logger = logging.getLogger("trader")
console = Console()

class TradingBot:
    def __init__(self):
        Config.validate()
        self.alpaca = AlpacaClient()
        self.llm = LLMEngine()
        self.risk = RiskManager()
        self.ta = TechnicalAnalysis()
        self.discovery = StockDiscovery()
        self.notif = NotificationManager(
            provider=Config.NOTIFY_PROVIDER,
            config=Config.get_notification_config()
        )
        self.running = False
        self.watchlist = list(self.discovery.discovered_stocks)
        self.last_market_state = False
        self.cycle_count = 0
        self.status_interval = 4
        self.start_date = datetime.now()
        self.last_summary_date = None
        self.starting_account_value = self.alpaca.get_portfolio_value()
        self.account_value = self.starting_account_value
        self.day_start_value = self.account_value
        self.total_deposited = max(self.starting_account_value, Config.SIMULATED_ACCOUNT_SIZE)
        # REALLOCATED: Using 100% of capital (was 50%) since options bot paused at micro-account size
        self.trading_capital_allocation = 1.0  # 100% (will revisit at $2k+)

    def run_cycle(self):
        self.cycle_count += 1
        self.account_value = self.alpaca.get_portfolio_value()
        self.trading_capital = self.account_value * self.trading_capital_allocation  # 100% while options paused
        market_open = self.alpaca.get_market_status()

        if market_open != self.last_market_state:
            if market_open:
                self.notif.notify_market_open()
                self.day_start_value = self.account_value
                logger.info("Market opened - starting daily tracking")
            else:
                self.notif.notify_market_close()
                self._send_daily_summary()
                logger.info("Market closed - sending notification")
            self.last_market_state = market_open

        if not market_open:
            logger.info("Market closed - skipping cycle")
            return

        console.print(Panel(f"[bold blue]Trading Cycle - {datetime.now().strftime('%H:%M:%S')}[/]", border_style="blue"))

        try:
            if self.should_discover_stocks():
                new_stocks = self.discovery.discover_trending_stocks()
                self.watchlist = new_stocks
                logger.info(f"Discovered {len(new_stocks)} stocks for watchlist")
                self.notif.send(f"Stock universe refreshed: {len(new_stocks)} stocks loaded")

            portfolio = self._gather_portfolio_data()
            positions = self.alpaca.get_positions()

            can_trade, reason = self.risk.can_trade(self.trading_capital)
            if not can_trade:
                logger.warning(f"Trading paused: {reason}")
                self.notif.notify_daily_loss_limit(self.risk.daily_pnl, Config.RISK_DAILY_LOSS_LIMIT / 100 * self.trading_capital)
                return

            stop_loss_orders = self.risk.check_stop_losses(positions)
            for order in stop_loss_orders:
                self.alpaca.close_position(order["symbol"])
                pnl_pct = (order["current_price"] - order["entry_price"]) / order["entry_price"] * 100
                dollar_pnl = (order["current_price"] - order["entry_price"]) * order["quantity"]
                self.risk.total_realized_pnl += dollar_pnl
                self.risk.record_trade(order["symbol"], "SELL (STOP LOSS)", order["quantity"], order["current_price"], pnl=pnl_pct)
                save_trade("trading", order["symbol"], "STOP LOSS", order["quantity"], entry_price=order["entry_price"], exit_price=order["current_price"], pnl_pct=pnl_pct, pnl_dollars=dollar_pnl)
                self.notif.notify_stop_loss(order["symbol"], order["entry_price"], order["current_price"], pnl_pct)

            expired = self.risk.get_expired_positions(positions)
            for sym in expired:
                pos = next((p for p in positions if p.symbol == sym), None)
                if not pos:
                    continue
                entry_p = float(pos.avg_entry_price)
                curr_p = float(pos.current_price)
                qty = float(pos.qty)
                pnl_pct = (curr_p / entry_p - 1) * 100
                dollar_pnl = (curr_p - entry_p) * qty
                self.alpaca.close_position(sym)
                self.risk.total_realized_pnl += dollar_pnl
                self.risk.record_trade(sym, "SELL (EXPIRY)", qty, curr_p, pnl=pnl_pct)
                save_trade("trading", sym, "EXPIRY CLOSE", qty, entry_price=entry_p, exit_price=curr_p, pnl_pct=pnl_pct, pnl_dollars=dollar_pnl)
                self.notif.send(f"Expired: sold {sym} after {Config.RISK_MAX_HOLDING_DAYS} days (PnL: {pnl_pct:+.2f}%)")

            decisions = self.llm.get_trading_decision(portfolio, account_value=self.trading_capital)
            if "error" in decisions:
                self.notif.send(f"LLM parse error: {decisions.get('summary')}", priority="high")
            self._execute_decisions(decisions, portfolio)

            self._print_status(portfolio)

            if self.cycle_count % self.status_interval == 0:
                risk_status = self.risk.get_status(portfolio['total_value'])
                self.notif.notify_status_update(
                    portfolio['total_value'],
                    portfolio['cash'],
                    portfolio['daily_pl'],
                    risk_status['daily_trades'],
                    risk_status['max_trades']
                )

        except Exception as e:
            logger.error(f"Cycle error: {e}", exc_info=True)

    def _send_daily_summary(self):
        from datetime import date as date_type
        today = date_type.today()
        if self.last_summary_date == today:
            return
        self.last_summary_date = today

        try:
            current_value = self.alpaca.get_portfolio_value()
            total_return_pct = ((current_value / self.starting_account_value) - 1) * 100
            days_elapsed = (datetime.now() - self.start_date).days
            trading_days = max(1, int(days_elapsed * 5 / 7))

            if days_elapsed > 0:
                apy = ((current_value / self.starting_account_value) ** (365 / max(1, days_elapsed)) - 1) * 100
            else:
                apy = 0

            spy_bars = self.alpaca.get_bars('SPY', days=max(3, days_elapsed + 5))
            spy_apy = 0
            spy_return_pct = 0
            if spy_bars is not None and len(spy_bars) > 1:
                spy_start = spy_bars['close'].iloc[0]
                spy_end = spy_bars['close'].iloc[-1]
                spy_return_pct = ((spy_end / spy_start) - 1) * 100
                if days_elapsed > 0:
                    spy_apy = ((spy_end / spy_start) ** (365 / max(1, days_elapsed)) - 1) * 100
                spy_value = self.starting_account_value * (1 + spy_return_pct / 100)

            edge = apy - spy_apy
            pos_count = len(self.alpaca.get_positions())

            wins = len([t for t in self.risk.trade_log if t.get('pnl', 0) >= 0])
            losses = len([t for t in self.risk.trade_log if t.get('pnl', 0) < 0])
            daily_pnl = current_value - self.day_start_value

            save_daily_snapshot("trading", self.day_start_value, current_value, daily_pnl, self.risk.daily_trades, wins, losses)

            if today.weekday() == 4:  # Friday
                weekly = generate_weekly_summary()
                if weekly:
                    self.notif.send(weekly, priority="low")

            self.notif.send(
                f"DAILY SUMMARY - {today.strftime('%b %d')}\n"
                f"Portfolio: ${current_value:.2f} (started ${self.starting_account_value:.0f})\n"
                f"Total Return: {total_return_pct:+.2f}% | APY: {apy:+.2f}%\n"
                f"Open Positions: {pos_count}\n"
                f"Today's Trades: {self.risk.daily_trades} (W:{wins}/L:{losses})\n"
                f"Day P&L: ${daily_pnl:+.2f}\n"
                f"\n"
                f"vs SPY Buy & Hold ($200 invested):\n"
                f"SPY Return: {spy_return_pct:+.2f}% | SPY Value: ${spy_value:.2f}\n"
                f"{'Our APY' if edge >= 0 else 'SPY APY'} leads by {abs(edge):.2f}%"
            )
        except Exception as e:
            logger.error(f"Daily summary failed: {e}")

    def should_discover_stocks(self) -> bool:
        if self.discovery.last_discovery_time is None:
            return True
        return datetime.now() - self.discovery.last_discovery_time > timedelta(hours=1)

    def _gather_portfolio_data(self) -> dict:
        total_value = self.alpaca.get_portfolio_value()
        cash = self.alpaca.get_cash()
        positions = self.alpaca.get_positions()

        technical_analysis = {}
        for symbol in self.watchlist[:100]:
            try:
                bars = self.alpaca.get_bars(symbol, days=3)
                if bars is not None and len(bars) > 50:
                    technical_analysis[symbol] = self.ta.compute_all(bars)
            except Exception as e:
                logger.debug(f"TA failed for {symbol}: {e}")

        return {
            "total_value": total_value,
            "cash": cash,
            "positions": [
                {"symbol": p.symbol, "qty": float(p.qty), "market_value": float(p.market_value),
                 "avg_entry_price": float(p.avg_entry_price), "unrealized_pl": float(p.unrealized_pl)}
                for p in positions
            ],
            "technical_analysis": technical_analysis,
            "timestamp": datetime.now().isoformat(),
            "market_open": True,
            "daily_pl": self.alpaca.get_daily_pl(),
            "trades_today": self.risk.daily_trades
        }

    def _execute_decisions(self, decisions: dict, portfolio: dict):
        total_deployed = sum(p["market_value"] for p in portfolio["positions"])

        for decision in decisions.get("decisions", []):
            symbol = decision.get("symbol")
            action = decision.get("action")
            quantity = decision.get("quantity", 0)
            strategy = decision.get("strategy", "unknown")

            if not symbol or not action:
                continue

            if symbol.upper() in Config.BLACKLIST:
                logger.info(f"Skipping {symbol} - blacklisted")
                continue

            price = self.alpaca.get_latest_price(symbol)
            if not price:
                logger.warning(f"No price data for {symbol}")
                continue

            decision["current_price"] = price

            MIN_NOTIONAL = 10
            if action == "BUY" and quantity > 0 and quantity * price < MIN_NOTIONAL:
                quantity = MIN_NOTIONAL / price
                decision["quantity"] = quantity
                logger.info(f"Lifted {symbol} qty to ${quantity * price:.0f} min notional (${MIN_NOTIONAL})")

            if action == "BUY" and quantity > 0:
                if total_deployed >= self.trading_capital:
                    logger.info(f"Rejecting BUY {symbol}: total deployed ${total_deployed:.2f} >= 50% cap (${self.trading_capital:.2f})")
                    continue
                max_shares = (self.trading_capital * Config.RISK_MAX_POSITION_PCT) / price
                cost = quantity * price
                if total_deployed + cost > self.trading_capital:
                    allowed = (self.trading_capital - total_deployed) / price
                    if allowed < 0.001:
                        logger.info(f"Rejecting BUY {symbol}: no room under 50% cap")
                        continue
                    logger.info(f"Capping {symbol} from ${cost:.2f} to ${allowed * price:.2f} (50% cap)")
                    quantity = allowed
                    decision["quantity"] = quantity
                if quantity > max_shares:
                    logger.info(f"Capping {symbol} from {quantity} to {max_shares:.4f} shares (max ${self.trading_capital * Config.RISK_MAX_POSITION_PCT:.0f} position)")
                    quantity = max_shares
                    decision["quantity"] = quantity
                total_deployed += quantity * price

            entry_price = None
            if action == "SELL" and symbol in self.risk.positions:
                entry_price = self.risk.positions[symbol]["entry_price"]

            approved, reason = self.risk.validate_order(
                decision, self.trading_capital, portfolio["cash"],
                self.alpaca.get_positions()
            )

            if not approved:
                logger.info(f"Rejected {action} {symbol} ({strategy}): {reason}")
                continue

            try:
                if action == "BUY" and quantity > 0:
                    stop_loss_price = price * (1 + Config.TA_STOP_LOSS_PCT)
                    use_bracket = quantity == int(quantity)
                    order_stop = stop_loss_price if use_bracket else None

                    self.alpaca.submit_market_order(symbol, OrderSide.BUY, quantity, stop_loss=order_stop)
                    self.risk.record_trade(symbol, "BUY", quantity, price, strategy=strategy)
                    self.notif.notify_trade("BUY", symbol, quantity, price)
                    save_trade("trading", symbol, "BUY", quantity, entry_price=price, strategy=strategy, reason=decision.get("reasoning"))
                    logger.info(f"BUY {quantity} {symbol} @ ${price:.2f} [{strategy}] stop=${stop_loss_price:.2f}{' [bracket]' if use_bracket else ' [software stop]'}")

                elif action == "SELL" and quantity > 0:
                    self.alpaca.submit_market_order(symbol, OrderSide.SELL, quantity)
                    if entry_price is not None:
                        dollar_pnl = (price - entry_price) * quantity
                        self.risk.total_realized_pnl += dollar_pnl
                        pnl_pct = (price - entry_price) / entry_price * 100
                        self.risk.record_trade(symbol, "SELL", quantity, price, pnl=pnl_pct, strategy=strategy)
                        save_trade("trading", symbol, "SELL", quantity, entry_price=entry_price, exit_price=price, pnl_pct=pnl_pct, pnl_dollars=dollar_pnl, strategy=strategy, stop_type="llm_signal")
                    else:
                        self.risk.record_trade(symbol, "SELL", quantity, price, strategy=strategy)
                        save_trade("trading", symbol, "SELL", quantity, exit_price=price, strategy=strategy, stop_type="llm_signal")
                    self.notif.notify_trade("SELL", symbol, quantity, price)
                    logger.info(f"SELL {quantity} {symbol} @ ${price:.2f} [{strategy}]")

            except Exception as e:
                logger.error(f"Order failed for {symbol}: {e}")

    def _print_status(self, portfolio: dict):
        risk_status = self.risk.get_status(portfolio['total_value'])

        console.print(Panel(
            f"Portfolio: ${portfolio['total_value']:.2f} | "
            f"Cash: ${portfolio['cash']:.2f} | "
            f"P&L: ${portfolio['daily_pl']:.2f} | "
            f"Trades: {risk_status['daily_trades']}/{risk_status['max_trades']} | "
            f"Watchlist: {len(self.watchlist)} stocks | "
            f"TA computed: {len(portfolio['technical_analysis'])} stocks | "
            f"Stop Loss: {Config.TA_STOP_LOSS_PCT:.0%}",
            title="Status",
            border_style="green"
        ))

    def start(self):
        strategy_label = f"Active (RSI {Config.TA_RSI_OVERSOLD:.0f}/{Config.TA_RSI_OVERBOUGHT:.0f}, MACD, BB, Trend)"
        stop_label = f"{Config.TA_STOP_LOSS_PCT:.0%}"
        console.print(Panel(
            f"[bold green]AI Trading Agent Started[/]\n"
            f"Discovery: Dynamic (100-stock universe + live trending)\n"
            f"Strategy: {strategy_label}\n"
            f"Stop Loss: {stop_label} | Max Position: {Config.RISK_MAX_POSITION_PCT:.0%}\n"
            f"Interval: {Config.TRADING_INTERVAL_MINUTES}min | Max trades/day: {Config.RISK_MAX_TRADES_PER_DAY}\n"
            f"Account: ${self.starting_account_value:.0f}",
            title="Bot Config",
            border_style="green"
        ))

        strategy_info = f"Active (RSI {Config.TA_RSI_OVERSOLD}/{Config.TA_RSI_OVERBOUGHT})"
        self.notif.notify_bot_startup(strategy_info)

        self.running = True
        schedule.every(Config.TRADING_INTERVAL_MINUTES).minutes.do(self.run_cycle)
        self.run_cycle()

        while self.running:
            schedule.run_pending()
            time.sleep(1)

    def stop(self):
        self.running = False
        console.print("[bold red]Bot stopped[/]")

if __name__ == "__main__":
    bot = TradingBot()
    try:
        bot.start()
    except KeyboardInterrupt:
        bot.stop()
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
