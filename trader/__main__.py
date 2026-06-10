import logging
import time
import schedule
import csv
import os
from datetime import datetime, timedelta
from alpaca.trading.enums import OrderSide
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
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
        self._daily_bars_cache = None
        self._daily_bars_date = None
        self._daily_spy_bars = None
        self._daily_spy_date = None
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
        # Options bot paused (2026-06-09, after a -$175 day); equity bot now gets the
        # full trading slice. Driven by Config so it's a single source of truth and
        # env-tunable — if options is ever re-enabled, drop this back below 1.0 and
        # make sure both bots' allocations sum to <= 1.0.
        self.trading_capital_allocation = Config.TRADING_CAPITAL_ALLOCATION
        logger.info(f"Initial seed: ${self.starting_account_value:.2f} | Deposits loaded: ${self.total_known_deposits:.2f} | Current equity: ${current_equity:.2f} | Account value (seed + PnL): ${self.account_value:.2f}")

    def _load_or_init_seed(self) -> float:
        """Return the immutable initial seed. Writes it once on first run."""
        seed_file = f"{os.getenv('DATA_DIR', '/app/data')}/initial_seed.txt"
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
        deposits_file = f"{os.getenv('DATA_DIR', '/app/data')}/deposits.csv"
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
                    len(self.discovery.discovered_stocks),
                    len(self.discovery.last_gainers),
                    len(self.discovery.last_losers),
                    len(self.discovery.last_active),
                )

            portfolio = self._gather_portfolio_data()
            positions = self.alpaca.get_positions()
            stock_positions = [p for p in positions if len(p.symbol) <= 10]
            self.risk.sync_from_alpaca(stock_positions)

            stock_unrealized = sum(float(p.unrealized_pl) for p in positions if len(p.symbol) <= 10)
            can_trade, reason = self.risk.can_trade(self.trading_capital, unrealized_pnl=stock_unrealized)
            if not can_trade:
                logger.warning(f"Trading paused: {reason}")
                self.notif.notify_daily_loss_limit(self.risk.daily_pnl + stock_unrealized, Config.RISK_DAILY_LOSS_LIMIT / 100 * self.trading_capital)
                # Log the paused cycle so monitoring doesn't go dark once the daily
                # trade cap / loss limit halts trading (these cycles return before the
                # normal cycle-logging point below).
                try:
                    from .tracker import log_cycle
                    log_cycle({
                        "bot": "trading",
                        "cycle": self.cycle_count,
                        "stage": "paused",
                        "reason": reason,
                        "equity": portfolio.get("total_value"),
                        "cash": portfolio.get("cash"),
                        "daily_pnl": self.risk.daily_pnl + stock_unrealized,
                        "trades_today": self.risk.daily_trades,
                    })
                except Exception as e:
                    logger.debug(f"paused-cycle logging failed: {e}")
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
            sys_prompt = llm_prompt.split("USER:\n", 1)[0].replace("SYSTEM:\n", "")
            usr_prompt = llm_prompt.split("USER:\n", 1)[1] if "USER:\n" in llm_prompt else llm_prompt
            self.email_notifier.send_llm_report(
                system_prompt=sys_prompt,
                user_prompt=usr_prompt,
                raw_response=llm_response,
                decisions=decisions,
                actions_taken=actions_taken
            )

            # Structured monitoring logs (best-effort; never blocks trading).
            try:
                from .tracker import log_cycle, log_llm_call
                log_llm_call(
                    "trading", getattr(self.llm, "last_model", None),
                    sys_prompt, usr_prompt, llm_response,
                    usage=getattr(self.llm, "last_usage", {}),
                    parse_ok="error" not in decisions,
                )
                ta = portfolio.get("technical_analysis", {})
                log_cycle({
                    "bot": "trading",
                    "cycle": self.cycle_count,
                    "equity": portfolio.get("total_value"),
                    "cash": portfolio.get("cash"),
                    "stock_mv": portfolio.get("stock_deployment_current"),
                    "stock_deploy_pct": portfolio.get("stock_deployment_pct"),
                    "num_positions": len([p for p in portfolio.get("positions", []) if len(p.get("symbol", "")) <= 10]),
                    "daily_pnl": portfolio.get("daily_pl"),
                    "trades_today": self.risk.daily_trades,
                    "regime_mode": portfolio.get("spy_regime_mode"),
                    "spy_rsi": portfolio.get("spy_rsi_14"),
                    "watchlist_size": len(self.watchlist),
                    "ta_computed": len(ta),
                    "top_candidates": [
                        {
                            "sym": s,
                            "buy_score": ta.get(s, {}).get("score", {}).get("buy_score"),
                            "sell_score": ta.get(s, {}).get("score", {}).get("sell_score"),
                            "rsi": ta.get(s, {}).get("rsi_14"),
                        }
                        for s in portfolio.get("top_candidates", [])
                    ],
                    "decisions": decisions.get("decisions", []),
                    "actions": actions_taken,
                    "market_outlook": decisions.get("market_outlook"),
                    "summary": decisions.get("summary"),
                })
            except Exception as e:
                logger.debug(f"cycle logging failed: {e}")

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

            spy_bars = self.alpaca.get_bars('SPY', days=250, timeframe=TimeFrame(1, TimeFrameUnit.Day))
            spy_apy = 0
            spy_return_pct = 0
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
        today = datetime.now().date()
        if self._daily_bars_date != today:
            bars_df = self.alpaca.get_bars_batch(self.watchlist[:100], days=250,
                                                  timeframe=TimeFrame(1, TimeFrameUnit.Day))
            self._daily_bars_cache = bars_df
            self._daily_bars_date = today
        else:
            bars_df = self._daily_bars_cache

        if bars_df is not None:
            # Drop the forming (today's) bar — only compute TA on completed daily bars.
            # Guard: only drop if the last timestamp is actually today (pre-open/pre-fetch
            # safety — if the cache fetched before today's bar formed, the last completed
            # bar is yesterday's and should be kept).
            timestamps = bars_df.index.get_level_values('timestamp').unique()
            if len(timestamps) > 1 and timestamps[-1].date() == today:
                bars_df = bars_df[bars_df.index.get_level_values('timestamp').isin(timestamps[:-1])]
            for symbol in self.watchlist[:100]:
                try:
                    if symbol in bars_df.index.get_level_values('symbol'):
                        symbol_bars = bars_df.xs(symbol, level=0)
                        if len(symbol_bars) > 60:
                            technical_analysis[symbol] = self.ta.compute_all(symbol_bars)
                except Exception as e:
                    logger.debug("TA failed for %s: %s", symbol, e)

        # Rank by momentum buy_score — shrink watchlist to 30 best candidates
        scored = [(s, d.get("score", {}).get("buy_score", 0)) for s, d in technical_analysis.items() if d]
        scored.sort(key=lambda x: -x[1])

        # Filter out hard-disqualified stocks: illiquid (volume<1000 or ratio<0.1)
        # or overbought (RSI>80).  These reached the LLM as ghost candidates
        # (THR with 132 vol, XLU at RSI 94) via the fallback at line 365-366.
        disqualified = set()
        for s, _ in scored:
            d = technical_analysis.get(s, {})
            vol = d.get("volume", {})
            rsi = d.get("rsi_14", 50)
            curr_vol = vol.get("current", 0) if isinstance(vol, dict) else 0
            vol_ratio = vol.get("ratio", 1.0) if isinstance(vol, dict) else 1.0
            if curr_vol < 1000 or vol_ratio < 0.1 or rsi > 80:
                disqualified.add(s)
        scored = [(s, bs) for s, bs in scored if s not in disqualified]

        self.watchlist = [s for s, _ in scored[:30]]
        logger.info(f"Ranked watchlist: {len(self.watchlist)} momentum names (top by buy_score, {len(disqualified)} disqualified)")

        # Identify top 5 candidates that meet the buy threshold for LLM focus
        buy_threshold = Config.TA_MIN_BUY_SCORE
        top_candidates = [s for s, _ in scored[:5] if technical_analysis[s].get("score", {}).get("buy_score", 0) >= buy_threshold]
        if not top_candidates and scored:
            top_candidates = [s for s, _ in scored[:5]]

        # Fetch Alpaca news headlines for top 5 candidates
        news_context = {}
        if top_candidates:
            from alpaca.data.historical.news import NewsClient
            from alpaca.data.requests import NewsRequest
            nc = NewsClient(Config.ALPACA_API_KEY, Config.ALPACA_SECRET_KEY)
            for sym in top_candidates:
                try:
                    req = NewsRequest(symbols=sym, limit=2, exclude_contentless=True)
                    articles = nc.get_news(req)
                    headlines = []
                    if hasattr(articles, "data"):
                        for article in articles.data.get(sym, []):
                            if hasattr(article, "headline") and article.headline:
                                headlines.append(article.headline)
                    news_context[sym] = headlines
                except Exception as e:
                    logger.debug("News fetch failed for %s: %s", sym, e)
                    news_context[sym] = []

        now = datetime.now()
        spy_rsi_14 = None
        spy_today = now.date()
        if self._daily_spy_date != spy_today:
            try:
                spy_bars_raw = self.alpaca.get_bars("SPY", days=250, timeframe=TimeFrame(1, TimeFrameUnit.Day))
                if spy_bars_raw is not None and len(spy_bars_raw) > 60:
                    # Drop forming bar — use only completed daily bars
                    timestamps = spy_bars_raw.index.get_level_values('timestamp').unique()
                    if len(timestamps) > 1 and timestamps[-1].date() == spy_today:
                        spy_bars_raw = spy_bars_raw[spy_bars_raw.index.get_level_values('timestamp').isin(timestamps[:-1])]
                    spy_rsi_14 = TechnicalAnalysis.rsi(spy_bars_raw["close"], 14)
                    self._daily_spy_bars = spy_bars_raw
                    self._daily_spy_date = spy_today
            except Exception as e:
                logger.debug("SPY RSI failed: %s", e)
        else:
            if self._daily_spy_bars is not None and len(self._daily_spy_bars) > 60:
                spy_rsi_14 = TechnicalAnalysis.rsi(self._daily_spy_bars["close"], 14)

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
                # in sync means a change to TARGET_POSITIONS scales both gates consistently.
                per_trade_size = self.trading_capital / Config.TARGET_POSITIONS
                max_position_value = max(50, min(per_trade_size, 2000))
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

            # Minimum-hold guard on VOLUNTARY sells. PDT no longer blocks same-day
            # round-trips (FINRA's June 4 2026 intraday-margin change), but our edge
            # is a DAILY-bar trend hold and the TA is cached once per day — so an
            # LLM sell on the same calendar day it bought is reacting to intraday
            # noise against an unchanged signal, not the validated signal. Block it.
            # Hard stops and expiry exits run on separate paths and are unaffected.
            if action == "SELL" and Config.RISK_MIN_HOLDING_DAYS > 0:
                entry_dt = self.risk.position_entry_dates.get(symbol)
                if entry_dt is not None:
                    held_days = (datetime.now().date() - entry_dt.date()).days
                    if held_days < Config.RISK_MIN_HOLDING_DAYS:
                        logger.info(f"Blocking voluntary SELL {symbol}: held {held_days}d < min {Config.RISK_MIN_HOLDING_DAYS}d")
                        action_results.append({"symbol": symbol, "action": action, "quantity": quantity, "price": price, "status": "rejected", "reason": f"Min holding period ({held_days}d < {Config.RISK_MIN_HOLDING_DAYS}d)"})
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
                    order = self.alpaca.submit_market_order(symbol, OrderSide.BUY, quantity, stop_loss=order_stop)
                    # Read actual fill price from order response. Market orders fill
                    # nearly instantly, but fall back to the pre-trade quote if still
                    # pending (filled_avg_price is None).
                    fill_price = float(getattr(order, "filled_avg_price", 0) or 0)
                    if fill_price <= 0:
                        fill_price = price
                    self.risk.register_position(symbol, fill_price, quantity)
                    self.risk.record_trade(symbol, "BUY", quantity, fill_price, strategy=strategy)
                    self.notif.notify_trade("BUY", symbol, quantity, fill_price)
                    save_trade("trading", symbol, "BUY", quantity, entry_price=fill_price, strategy=strategy, reason=decision.get("reasoning"))
                    stock_mv += quantity * fill_price  # track for subsequent buys in this cycle
                    logger.info(f"BUY {quantity} {symbol} @ ${fill_price:.2f} [{strategy}] stop=${stop_loss_price:.2f}{' [bracket]' if use_bracket else ' [software stop]'}")
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
            f"Stop Loss: {stop_label} | Max Position: ${max(50, min(self.starting_account_value * Config.TRADING_CAPITAL_ALLOCATION / Config.TARGET_POSITIONS, 2000)):.0f}\n"
            f"Interval: {Config.TRADING_INTERVAL_MINUTES}min | Max trades/day: {Config.RISK_MAX_TRADES_PER_DAY}\n"
            f"Account: ${self.starting_account_value:.0f}",
            title="Bot Config",
            border_style="green"
        ))

        strategy_info = f"Active (RSI {Config.TA_RSI_OVERSOLD}/{Config.TA_RSI_OVERBOUGHT})"
        self.notif.notify_bot_startup(strategy_info, account_value=self.starting_account_value)

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
