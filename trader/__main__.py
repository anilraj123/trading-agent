import logging
import time
import schedule
import csv
import os
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
from .tracker import save_daily_snapshot, save_trade, save_discovery_snapshot, generate_weekly_summary
from .email_notifier import EmailNotifier

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
        self.email_notifier = EmailNotifier()
        self.running = False
        self.watchlist = list(self.discovery.discovered_stocks)
        self.last_market_state = False
        self.cycle_count = 0
        self.status_interval = 4
        self.start_date = datetime.now()
        self.last_summary_date = None
        # initial_seed is the immutable "principal at first-ever bot startup". It is
        # persisted on disk so that restarts AFTER a deposit don't snapshot the new
        # (deposit-inflated) equity as the baseline. Without this, deposits would be
        # double-counted: once via the inflated starting_account_value and again via
        # the deposits.csv subtraction below.
        self.starting_account_value = self._load_or_init_seed()
        # account_value tracks "principal lineage + realized/unrealized P&L" (i.e.,
        # excludes deposits). trading_capital sizes risk off this rather than raw
        # Alpaca equity so cash injections don't auto-inflate position sizes.
        self.known_deposits = self._load_deposits_csv()
        self.total_known_deposits = sum(self.known_deposits)
        current_equity = self.alpaca.get_portfolio_value()
        self.account_value = current_equity - self.total_known_deposits

        init_positions = self.alpaca.get_positions()
        init_stock_positions = [p for p in init_positions if len(p.symbol) <= 10]
        self.risk.sync_from_alpaca(init_stock_positions)
        self.day_start_value = self.account_value
        # REALLOCATED: 60% to trading, 40% to options (restarted at ~$1.7k equity)
        self.trading_capital_allocation = 0.60  # 60% trading, 40% options
        logger.info(f"Initial seed: ${self.starting_account_value:.2f} | Deposits loaded: ${self.total_known_deposits:.2f} | Current equity: ${current_equity:.2f} | Account value (seed + PnL): ${self.account_value:.2f}")

    def _load_or_init_seed(self) -> float:
        """Return the immutable initial seed. Writes it once on first run."""
        seed_file = "/app/data/initial_seed.txt"
        if os.path.exists(seed_file):
            try:
                with open(seed_file) as f:
                    return float(f.read().strip())
            except Exception as e:
                logger.error(f"Failed to read initial_seed.txt ({e}); falling back to current equity (will be re-written)")
        seed = self.alpaca.get_portfolio_value()
        try:
            os.makedirs(os.path.dirname(seed_file), exist_ok=True)
            with open(seed_file, "w") as f:
                f.write(f"{seed:.2f}")
            logger.info(f"Initialized seed file at ${seed:.2f}")
        except Exception as e:
            logger.error(f"Failed to write initial_seed.txt: {e}")
        return seed
    
    def _load_deposits_csv(self):
        """Load known deposits from deposits.csv."""
        deposits = []
        deposits_file = "/app/data/deposits.csv"
        if not os.path.exists(deposits_file):
            logger.warning("deposits.csv not found, assuming no prior deposits")
            return deposits
        try:
            with open(deposits_file) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    deposits.append(float(row["amount"]))
            logger.info(f"Loaded {len(deposits)} deposits from CSV: ${sum(deposits):.2f} total")
        except Exception as e:
            logger.error(f"Failed to load deposits.csv: {e}")
        return deposits
    
    def run_cycle(self):
        self.cycle_count += 1
        current_equity = self.alpaca.get_portfolio_value()
        
        # Calculate true trading capital (excluding manually logged deposits only)
        total_deposits = self.total_known_deposits
        self.account_value = current_equity - total_deposits
        self.trading_capital = self.account_value * self.trading_capital_allocation
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
                save_discovery_snapshot(
                    len(new_stocks),
                    len(self.discovery.discovered_stocks) if hasattr(self.discovery, 'discovered_stocks') else 0,
                    len(self.discovery.last_gainers) if hasattr(self.discovery, 'last_gainers') and self.discovery.last_gainers else 0,
                    len(self.discovery.last_losers) if hasattr(self.discovery, 'last_losers') and self.discovery.last_losers else 0,
                    len(self.discovery.last_active) if hasattr(self.discovery, 'last_active') and self.discovery.last_active else 0,
                )

            portfolio = self._gather_portfolio_data()
            positions = self.alpaca.get_positions()
            stock_positions = [p for p in positions if len(p.symbol) <= 10]
            self.risk.sync_from_alpaca(stock_positions)

            can_trade, reason = self.risk.can_trade(self.trading_capital)
            if not can_trade:
                logger.warning(f"Trading paused: {reason}")
                self.notif.notify_daily_loss_limit(self.risk.daily_pnl, Config.RISK_DAILY_LOSS_LIMIT / 100 * self.trading_capital)
                return

            stop_loss_orders = self.risk.check_stop_losses(positions)
            for order in stop_loss_orders:
                self.alpaca.close_position(order["symbol"])
                # Drop the risk-manager entry so the next cycle doesn't try to fire a
                # second stop on a position that's already been closed.
                self.risk.unregister_position(order["symbol"])
                pnl_pct = (order["current_price"] - order["entry_price"]) / order["entry_price"] * 100
                dollar_pnl = (order["current_price"] - order["entry_price"]) * order["quantity"]
                self.risk.total_realized_pnl += dollar_pnl
                # Forced exit (stop-loss): don't count toward the daily voluntary-trade cap.
                self.risk.record_trade(order["symbol"], "SELL (STOP LOSS)", order["quantity"], order["current_price"], pnl=pnl_pct, pnl_dollars=dollar_pnl, counts_toward_daily_cap=False)
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
                self.risk.unregister_position(sym)
                self.risk.total_realized_pnl += dollar_pnl
                # Forced exit (holding-period expiry): don't count toward the daily voluntary-trade cap.
                self.risk.record_trade(sym, "SELL (EXPIRY)", qty, curr_p, pnl=pnl_pct, pnl_dollars=dollar_pnl, counts_toward_daily_cap=False)
                save_trade("trading", sym, "EXPIRY CLOSE", qty, entry_price=entry_p, exit_price=curr_p, pnl_pct=pnl_pct, pnl_dollars=dollar_pnl)
                self.notif.send(f"Expired: sold {sym} after {Config.RISK_MAX_HOLDING_DAYS} days (PnL: {pnl_pct:+.2f}%)")

            spy_rsi = portfolio.get("spy_rsi_14")
            if spy_rsi is not None and spy_rsi < 30:
                logger.warning(f"SPY regime: HARD BLOCK (RSI={spy_rsi:.1f})")
                portfolio["spy_regime_mode"] = "blocked"
            elif spy_rsi is not None and spy_rsi < 40:
                logger.info(f"SPY regime: REDUCED (RSI={spy_rsi:.1f}, 50% position size, min buy_score 3.0)")
                portfolio["spy_regime_mode"] = "reduced"
            else:
                portfolio["spy_regime_mode"] = "normal"
            self._last_regime_mode = portfolio["spy_regime_mode"]

            decisions, llm_prompt, llm_response = self.llm.get_trading_decision(portfolio, account_value=self.trading_capital)
            if "error" in decisions:
                self.notif.send(f"LLM parse error: {decisions.get('summary')}", priority="high")
            try:
                actions_taken = self._execute_decisions(decisions, portfolio)
            except Exception as e:
                logger.error(f"Executing decisions failed (LLM report still sent): {e}")
                actions_taken = []
            self.email_notifier.send_llm_report(
                system_prompt=llm_prompt.split("USER:\n", 1)[0].replace("SYSTEM:\n", ""),
                user_prompt=llm_prompt.split("USER:\n", 1)[1] if "USER:\n" in llm_prompt else llm_prompt,
                raw_response=llm_response,
                decisions=decisions,
                actions_taken=actions_taken
            )

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

            # true_trading_pnl  = current equity − everything we ever put in (seed + deposits)
            # principal_invested is the denominator for "% return on capital": you'd be flat
            # if return_pct = 0 (current_value == principal). Using just the seed as the
            # denominator (the prior behavior) overstated returns after any deposit.
            total_deposits = self.total_known_deposits
            principal_invested = self.starting_account_value + total_deposits
            true_trading_pnl = current_value - principal_invested
            true_return_pct = (true_trading_pnl / max(principal_invested, 1)) * 100
            total_return_pct = ((current_value / max(principal_invested, 1)) - 1) * 100
            
            days_elapsed = (datetime.now() - self.start_date).days

            if days_elapsed > 0 and true_return_pct != 0:
                apy = ((1 + true_return_pct / 100) ** (365 / max(1, days_elapsed)) - 1) * 100
            else:
                apy = 0

            spy_bars = self.alpaca.get_bars('SPY')
            spy_apy = 0
            spy_return_pct = 0
            # Default spy_value to starting equity (i.e., "SPY flat") so the f-string below
            # can't NameError if the SPY bars fetch fails. The outer except would otherwise
            # swallow the error and silently drop the entire daily summary.
            spy_value = self.starting_account_value
            if spy_bars is not None and len(spy_bars) > 1:
                spy_start = spy_bars['close'].iloc[0]
                spy_end = spy_bars['close'].iloc[-1]
                spy_return_pct = ((spy_end / spy_start) - 1) * 100
                if days_elapsed > 0:
                    spy_apy = ((spy_end / spy_start) ** (365 / max(1, days_elapsed)) - 1) * 100
                spy_value = self.starting_account_value * (1 + spy_return_pct / 100)

            edge = apy - spy_apy
            raw_positions = self.alpaca.get_positions()
            pos_count = len(raw_positions)

            wins = len([t for t in self.risk.trade_log if t.get('pnl', 0) >= 0])
            losses = len([t for t in self.risk.trade_log if t.get('pnl', 0) < 0])
            # Stock-only daily P&L: exclude options positions so theta decay doesn't
            # distort the stock bot's performance tracking. Options operate on a
            # completely different time horizon (7-35 DTE) and their daily P&L is
            # tracked separately by the options bot.
            stock_unrealized = sum(float(p.unrealized_pl) for p in raw_positions if len(p.symbol) <= 10)
            daily_pnl = stock_unrealized + self.risk.daily_pnl

            save_daily_snapshot("trading", self.day_start_value, current_value, daily_pnl, self.risk.daily_trades, wins, losses, total_deposited=total_deposits)

            positions_dict = [
                {"symbol": p.symbol, "qty": float(p.qty), "market_value": float(p.market_value),
                 "avg_entry_price": float(p.avg_entry_price), "unrealized_pl": float(p.unrealized_pl)}
                for p in raw_positions
            ]

            if today.weekday() == 4:  # Friday
                weekly = generate_weekly_summary()
                if weekly:
                    self.notif.send(weekly, priority="low")
                    self.email_notifier.send_weekly_report(
                        total_deposits, self.starting_account_value, current_value,
                        self.risk.trade_log, true_trading_pnl
                    )

            self.email_notifier.send_daily_report(
                self.day_start_value, current_value, daily_pnl, total_deposits,
                true_trading_pnl, self.risk.daily_trades, wins, losses,
                positions_dict, spy_return_pct, apy, spy_apy
            )

            # Fetch the regime mode that was set during the last cycle.
            regime_mode = getattr(self, '_last_regime_mode', 'normal')
            self.notif.send(
                f"DAILY SUMMARY - {today.strftime('%b %d')}\n"
                f"Portfolio: ${current_value:.2f}\n"
                f"Deposits: ${total_deposits:.2f} | Trading P&L: ${true_trading_pnl:+.2f} | Return: {true_return_pct:+.2f}% | APY: {apy:+.2f}%\n"
                f"Open Positions: {pos_count}\n"
                f"Today's Trades: {self.risk.daily_trades} (W:{wins}/L:{losses})\n"
                f"Day P&L: ${daily_pnl:+.2f}\n"
                f"Regime: {regime_mode.upper()}\n"
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
        stock_unrealized = sum(float(p.unrealized_pl) for p in positions if len(p.symbol) <= 10)
        stock_daily_pl = stock_unrealized + self.risk.daily_pnl

        stock_mv = sum(float(p.qty) * float(p.current_price) for p in positions if len(p.symbol) <= 10)
        max_stock_deploy = cash * Config.MAX_STOCK_DEPLOYMENT_PCT
        stock_deploy_pct = (stock_mv / max_stock_deploy * 100) if max_stock_deploy > 0 else 0

        technical_analysis = {}
        for symbol in self.watchlist[:100]:
            try:
                bars = self.alpaca.get_bars(symbol)
                if bars is not None and len(bars) > 50:
                    technical_analysis[symbol] = self.ta.compute_all(bars)
            except Exception as e:
                logger.debug(f"TA failed for {symbol}: {e}")

        # Rank by momentum buy_score — shrink watchlist to 30 best candidates
        scored = [(s, d.get("score", {}).get("buy_score", 0)) for s, d in technical_analysis.items() if d]
        scored.sort(key=lambda x: -x[1])
        self.watchlist = [s for s, _ in scored[:30]]
        logger.info(f"Ranked watchlist: {len(self.watchlist)} momentum names (top by buy_score)")

        # Identify top 5 candidates that meet the buy threshold for LLM focus
        buy_threshold = Config.TA_MIN_BUY_SCORE
        top_candidates = [s for s, _ in scored[:5] if technical_analysis[s].get("score", {}).get("buy_score", 0) >= buy_threshold]
        if not top_candidates and scored:
            top_candidates = [s for s, _ in scored[:5]]

        # Fetch Alpaca news headlines for top 5 candidates
        news_context = {}
        if top_candidates:
            for sym in top_candidates:
                try:
                    from alpaca.data.historical.news import NewsClient
                    from alpaca.data.requests import NewsRequest
                    nc = NewsClient(Config.ALPACA_API_KEY, Config.ALPACA_SECRET_KEY)
                    req = NewsRequest(symbols=sym, limit=2, exclude_contentless=True)
                    articles = nc.get_news(req)
                    headlines = []
                    if hasattr(articles, "data"):
                        for article in articles.data.get(sym, []):
                            if hasattr(article, "headline") and article.headline:
                                headlines.append(article.headline)
                    news_context[sym] = headlines
                except Exception as e:
                    logger.debug(f"News fetch failed for {sym}: {e}")
                    news_context[sym] = []

        spy_rsi_14 = None
        try:
            spy_bars = self.alpaca.get_bars("SPY")
            if spy_bars is not None and len(spy_bars) > 20:
                spy_rsi_14 = TechnicalAnalysis.rsi(spy_bars["close"], 14)
        except Exception as e:
            logger.debug(f"SPY RSI failed: {e}")

        return {
            "total_value": total_value,
            "cash": cash,
            "spy_rsi_14": spy_rsi_14,
            "spy_regime_mode": "normal",
            "top_candidates": top_candidates,
            "news_context": news_context,
            "positions": [
                {"symbol": p.symbol, "qty": float(p.qty), "market_value": float(p.market_value),
                 "avg_entry_price": float(p.avg_entry_price), "unrealized_pl": float(p.unrealized_pl)}
                for p in positions
            ],
            "technical_analysis": technical_analysis,
            "timestamp": datetime.now().isoformat(),
            "market_open": True,
            "daily_pl": stock_daily_pl,
            "trades_today": self.risk.daily_trades,
            "stock_deployment_current": round(stock_mv, 2),
            "stock_deployment_max": round(max_stock_deploy, 2),
            "stock_deployment_pct": round(stock_deploy_pct, 1),
            "stock_deployment_at_cap": stock_mv >= max_stock_deploy * 0.95
        }

    def _execute_decisions(self, decisions: dict, portfolio: dict) -> list:
        action_results = []

        # Total stock deployment cap: never use more than N% of cash for stock positions.
        # The remainder acts as an options buying power buffer.
        positions = self.alpaca.get_positions()
        stock_mv = sum(float(p.qty) * float(p.current_price) for p in positions if len(p.symbol) <= 10)
        total_cash = portfolio["cash"]

        for decision in decisions.get("decisions", []):
            symbol = decision.get("symbol")
            action = decision.get("action")
            quantity = decision.get("quantity", 0)
            strategy = decision.get("strategy", "unknown")

            if not symbol or not action:
                continue

            sym_upper = symbol.upper()
            if sym_upper in Config.BLACKLIST:
                logger.info(f"Skipping {symbol} - blacklisted")
                action_results.append({"symbol": symbol, "action": action, "quantity": quantity, "price": 0, "status": "rejected", "reason": "Blacklisted"})
                continue

            if any(sym_upper.endswith(suffix) for suffix in Config.CRYPTO_SUFFIXES):
                logger.info(f"Rejecting {symbol} - crypto asset not allowed")
                action_results.append({"symbol": symbol, "action": action, "quantity": quantity, "price": 0, "status": "rejected", "reason": "Crypto not allowed"})
                continue

            price = self.alpaca.get_latest_price(symbol)
            if not price:
                logger.warning(f"No price data for {symbol}")
                action_results.append({"symbol": symbol, "action": action, "quantity": quantity, "price": 0, "status": "rejected", "reason": "No price data"})
                continue

            decision["current_price"] = price

            MIN_NOTIONAL = 10
            if action == "BUY" and quantity > 0 and quantity * price < MIN_NOTIONAL:
                quantity = MIN_NOTIONAL / price
                decision["quantity"] = quantity
                logger.info(f"Lifted {symbol} qty to ${quantity * price:.0f} min notional (${MIN_NOTIONAL})")

            if action == "BUY" and quantity > 0:
                # Stock deployment cap: never exceed N% of cash in total stock market value.
                # This reserves the remaining cash as an options buying power buffer.
                cost = quantity * price
                max_stock_deploy = total_cash * Config.MAX_STOCK_DEPLOYMENT_PCT
                headroom = max_stock_deploy - stock_mv
                if cost > headroom:
                    if headroom <= 0:
                        logger.info(f"Rejecting BUY {symbol}: stock deployment at cap (${stock_mv:.2f} / ${max_stock_deploy:.2f})")
                        action_results.append({"symbol": symbol, "action": action, "quantity": quantity, "price": price, "status": "rejected", "reason": "Stock deployment cap reached"})
                        continue
                    capped_qty = headroom / price
                    if capped_qty < 0.001:
                        logger.info(f"Rejecting BUY {symbol}: insufficient headroom ${headroom:.2f}")
                        action_results.append({"symbol": symbol, "action": action, "quantity": quantity, "price": price, "status": "rejected", "reason": "Insufficient stock deployment headroom"})
                        continue
                    logger.info(f"Capping {symbol} from ${cost:.2f} to ${capped_qty * price:.2f} (stock deployment cap: ${stock_mv:.2f}/${max_stock_deploy:.2f})")
                    quantity = capped_qty
                    decision["quantity"] = quantity

                # Soft cash reservation: both bots share one Alpaca account, so each only
                # "sees" its allocation slice of the cash pool. Without this, whichever bot
                # ran first could consume cash earmarked for the other and the second bot
                # would get rejected with "No cash available". This is a soft cap — nothing
                # physically prevents overspend, but in steady state each bot stays in its
                # lane and races are eliminated.
                available_cash = portfolio["cash"] * self.trading_capital_allocation
                # Anchor the soft cap to trading_capital (same anchor RiskManager.validate_order uses).
                # Previously this used self.account_value, which is the un-allocated base — that made
                # the soft cap looser than the validator's, so it was effectively dead. Keeping them
                # in sync means a change to RISK_MAX_POSITION_PCT scales both gates consistently.
                max_position_value = self.trading_capital * Config.RISK_MAX_POSITION_PCT
                cost = quantity * price

                if cost > available_cash:
                    capped_qty = available_cash / price
                    if capped_qty < 0.001:
                        logger.info(f"Rejecting BUY {symbol}: no cash (${available_cash:.2f})")
                        action_results.append({"symbol": symbol, "action": action, "quantity": quantity, "price": price, "status": "rejected", "reason": "No cash available"})
                        continue
                    logger.info(f"Capping {symbol} from ${cost:.2f} to ${capped_qty * price:.2f} (cash limit)")
                    quantity = capped_qty
                    decision["quantity"] = quantity

                if cost > max_position_value:
                    max_qty = max_position_value / price
                    logger.info(f"Capping {symbol} from {quantity} to {max_qty:.4f} (max ${max_position_value:.0f} position)")
                    quantity = max_qty
                    decision["quantity"] = quantity

            entry_price = None
            if action == "SELL" and symbol in self.risk.positions:
                entry_price = self.risk.positions[symbol]["entry_price"]

            # SPY regime filter: tiered response to broad market weakness.
            regime = portfolio.get("spy_regime_mode", "normal")
            if action == "BUY" and regime == "blocked":
                logger.info(f"Rejecting BUY {symbol}: SPY hard block (RSI < 30)")
                action_results.append({"symbol": symbol, "action": action, "quantity": quantity, "price": price, "status": "rejected", "reason": "SPY hard block"})
                continue
            if action == "BUY" and regime == "reduced":
                ta = portfolio.get("technical_analysis", {})
                sym_ta = ta.get(symbol, {})
                sym_buy_score = sym_ta.get("score", {}).get("buy_score", 0) if sym_ta else 0
                if sym_buy_score < 3.0:
                    logger.info(f"Rejecting BUY {symbol}: SPY reduced regime — buy_score {sym_buy_score:.2f} < 3.0")
                    action_results.append({"symbol": symbol, "action": action, "quantity": quantity, "price": price, "status": "rejected", "reason": "SPY reduced regime"})
                    continue
                quantity = round(quantity * 0.5, 4)
                decision["quantity"] = quantity
                logger.info(f"SPY reduced regime: halved {symbol} qty to {quantity} (buy_score={sym_buy_score:.2f})")

            # PDT guard: block same-day sells. Positions opened today cannot be
            # sold until the next trading day. Prevents day-trading flag risk and
            # stops the bot from opening positions with no exit path.
            if action == "SELL":
                entry_dt = self.risk.position_entry_dates.get(symbol)
                if entry_dt and entry_dt.date() == datetime.now().date():
                    logger.info(f"Rejecting SELL {symbol}: bought today ({entry_dt.strftime('%H:%M')}), PDT lock until next session")
                    action_results.append({"symbol": symbol, "action": action, "quantity": quantity, "price": price, "status": "rejected", "reason": "PDT lock: bought today"})
                    continue

            # Pass the trader's reserved cash slice (not raw account cash) so the
            # validator's "not enough cash" check matches the soft reservation above.
            approved, reason = self.risk.validate_order(
                decision, self.trading_capital, portfolio["cash"] * self.trading_capital_allocation,
                self.alpaca.get_positions()
            )

            if not approved:
                logger.info(f"Rejected {action} {symbol} ({strategy}): {reason}")
                action_results.append({"symbol": symbol, "action": action, "quantity": quantity, "price": price, "status": "rejected", "reason": reason})
                continue

            try:
                if action == "BUY" and quantity > 0:
                    stop_loss_price = price * (1 + Config.TA_STOP_LOSS_PCT)
                    use_bracket = quantity == int(quantity)
                    order_stop = stop_loss_price if use_bracket else None

                    # Submit FIRST. Only register the position with the risk manager
                    # after Alpaca accepts the order — otherwise a failed submit leaves
                    # a phantom entry that the software-stop loop will react to.
                    self.alpaca.submit_market_order(symbol, OrderSide.BUY, quantity, stop_loss=order_stop)
                    self.risk.register_position(symbol, price, quantity)
                    self.risk.record_trade(symbol, "BUY", quantity, price, strategy=strategy)
                    self.notif.notify_trade("BUY", symbol, quantity, price)
                    save_trade("trading", symbol, "BUY", quantity, entry_price=price, strategy=strategy, reason=decision.get("reasoning"))
                    stock_mv += quantity * price  # track for subsequent buys in this cycle
                    logger.info(f"BUY {quantity} {symbol} @ ${price:.2f} [{strategy}] stop=${stop_loss_price:.2f}{' [bracket]' if use_bracket else ' [software stop]'}")
                    action_results.append({"symbol": symbol, "action": action, "quantity": quantity, "price": price, "status": "executed", "reason": f"stop={stop_loss_price:.2f} bracket={use_bracket}"})

                elif action == "SELL" and quantity > 0:
                    self.alpaca.submit_market_order(symbol, OrderSide.SELL, quantity)
                    # Unregister only after submit succeeds, so a failed sell doesn't
                    # silently drop the stop-loss tracking on a position we still hold.
                    self.risk.unregister_position(symbol)
                    if entry_price is not None:
                        dollar_pnl = (price - entry_price) * quantity
                        self.risk.total_realized_pnl += dollar_pnl
                        pnl_pct = (price - entry_price) / entry_price * 100
                        self.risk.record_trade(symbol, "SELL", quantity, price, pnl=pnl_pct, pnl_dollars=dollar_pnl, strategy=strategy)
                        save_trade("trading", symbol, "SELL", quantity, entry_price=entry_price, exit_price=price, pnl_pct=pnl_pct, pnl_dollars=dollar_pnl, strategy=strategy, stop_type="llm_signal")
                    else:
                        # No tracked entry (likely a position opened before bot restart). Skip P&L attribution.
                        self.risk.record_trade(symbol, "SELL", quantity, price, strategy=strategy)
                        save_trade("trading", symbol, "SELL", quantity, exit_price=price, strategy=strategy, stop_type="llm_signal")
                    self.notif.notify_trade("SELL", symbol, quantity, price)
                    logger.info(f"SELL {quantity} {symbol} @ ${price:.2f} [{strategy}]")
                    action_results.append({"symbol": symbol, "action": action, "quantity": quantity, "price": price, "status": "executed", "reason": f"pnl={pnl_pct:+.2f}%" if entry_price else ""})

            except Exception as e:
                logger.error(f"Order failed for {symbol}: {e}")
                action_results.append({"symbol": symbol, "action": action, "quantity": quantity, "price": price, "status": "failed", "reason": str(e)})

        return action_results

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
