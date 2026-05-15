import logging
import time
import json
import schedule
from datetime import datetime, timedelta, date
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest, GetOptionContractsRequest
from alpaca.data import OptionHistoricalDataClient
from alpaca.data.requests import OptionSnapshotRequest

from trader.config import Config
from trader.alpaca_client import AlpacaClient
from trader.llm_engine import LLMEngine
from trader.notifications import NotificationManager as BaseNotif
from trader.stock_discovery import StockDiscovery, UNIVERSE_100
from trader.tracker import save_daily_snapshot, save_trade, generate_weekly_summary

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="[%H:%M:%S]")
logger = logging.getLogger("options")

ALLOCATED_PCT = 0.50           # 50% of equity allocated to options
PER_POSITION_PCT = 0.08        # 8% of allocated per position
TOTAL_DEPLOYED_PCT = 0.50      # 50% of allocated total cap
TARGET_GAIN_PCT = 50
CONTRACT_DTE_MIN = 7
CONTRACT_DTE_MAX = 35
OPTIONS_WATCHLIST_SIZE = 50    # Expanded from 30 for broader symbol coverage
MIN_OPTION_OI = 500            # minimum open interest for liquidity
MAX_OPTION_SPREAD = 0.50       # maximum bid-ask spread ($) — relaxed from 0.20 for viability
                                # At $35 cost, 0.50 spread = 1.4% slippage (acceptable for 50% target)

class NotificationManager(BaseNotif):
    def send(self, message, priority="normal"):
        msg = f"[OPTIONS] {message}"
        super().send(msg, priority)


def _quote_option(client, symbol):
    try:
        snap = client.get_option_snapshot(OptionSnapshotRequest(symbol_or_symbols=symbol))
        if isinstance(snap, dict) and symbol in snap:
            snap = snap[symbol]
        if isinstance(snap, dict):
            q = snap.get("latest_quote") or snap.get("quote")
        else:
            q = getattr(snap, "latest_quote", None) or getattr(snap, "quote", None)
        if not q:
            return None, None
        if isinstance(q, dict):
            return float(q.get("bid_price") or q.get("bid") or 0), float(q.get("ask_price") or q.get("ask") or 0)
        return float(getattr(q, "bid_price", 0) or 0), float(getattr(q, "ask_price", 0) or 0)
    except:
        return None, None


def _underlying_price(symbol):
    try:
        from alpaca.data import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        stock_data = StockHistoricalDataClient(Config.ALPACA_API_KEY, Config.ALPACA_SECRET_KEY)
        bars = stock_data.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
            start=date.today() - timedelta(days=5)
        ))
        if not bars.df.empty:
            return float(bars.df["close"].iloc[-1])
    except: pass
    return None


def _option_dte(symbol):
    try:
        date_str = symbol[-15:-9]
        exp = datetime.strptime(date_str, "%y%m%d").date()
        return (exp - date.today()).days
    except:
        return None

def _get_dynamic_stop(dte):
    if dte is None:
        return -0.80
    if dte <= 5:
        return -0.25
    elif dte <= 14:
        return -0.40
    else:
        return -0.55

def _force_exit_near_expiry(dte):
    if dte is None:
        return False
    if dte <= 3:
        now = datetime.now()
        if now.hour >= 15:  # within last hour of market (market closes 4pm ET)
            return True
    return False

def _has_viable_option(trading_client, data_client, symbol, budget):
    today_d = date.today()
    price = _underlying_price(symbol)
    if not price:
        return False
    
    # Track rejection reasons for diagnostics
    total_contracts = 0
    rejected = {"no_price": 0, "itm": 0, "low_oi": 0, "wide_spread": 0, "budget": 0, "otm_violation": 0}
    
    for start_dte, end_dte in [(7, 10), (11, 14), (15, 18), (19, 21), (22, 25), (26, 28), (29, 32), (33, CONTRACT_DTE_MAX)]:
        try:
            req = GetOptionContractsRequest(
                underlying_symbols=[symbol], status="active",
                expiration_date_gte=(today_d + timedelta(days=start_dte)).isoformat(),
                expiration_date_lte=(today_d + timedelta(days=end_dte)).isoformat()
            )
            resp = trading_client.get_option_contracts(req)
            if not hasattr(resp, "option_contracts"):
                continue
            for c in resp.option_contracts:
                try:
                    total_contracts += 1
                    strike = float(c.strike_price)
                    if c.type not in ("call", "put"):
                        continue
                    is_otm = (c.type == "call" and strike > price) or (c.type == "put" and strike < price)
                    if not is_otm:
                        rejected["itm"] += 1
                        continue
                    oi = getattr(c, "open_interest", 0) or 0
                    if oi < MIN_OPTION_OI:
                        rejected["low_oi"] += 1
                        continue
                    bid, ask = _quote_option(data_client, c.symbol)
                    if not bid or not ask:
                        rejected["no_price"] += 1
                        continue
                    if ask - bid > MAX_OPTION_SPREAD:
                        rejected["wide_spread"] += 1
                        continue
                    mid = (bid + ask) / 2
                    if mid <= 0 or mid * 100 > budget:
                        rejected["budget"] += 1
                        continue
                    dte = (c.expiration_date - today_d).days
                    otm_pct = (strike / price - 1) * 100 if c.type == "call" else (1 - strike / price) * 100
                    if dte < 15 and abs(otm_pct) > 5:
                        rejected["otm_violation"] += 1
                        continue
                    # Found viable contract!
                    logger.debug(f"{symbol}: Found viable {c.type} ${strike:.0f} @ ${mid:.2f} ({dte} DTE, {oi} OI)")
                    return True
                except:
                    pass
        except:
            pass
    
    # Log rejection details for debugging (DEBUG level, only shown if enabled)
    if total_contracts > 0:
        logger.debug(f"{symbol}: {total_contracts} contracts found, none viable. Rejections: spread={rejected['wide_spread']}, oi={rejected['low_oi']}, budget={rejected['budget']}, otm%={rejected['otm_violation']}, itm={rejected['itm']}")
    return False


def _find_contract(trading_client, data_client, symbol, direction, budget):
    today_d = date.today()
    price = _underlying_price(symbol)
    if not price: return None

    all_contracts = []
    for start_dte, end_dte in [(7, 10), (11, 14), (15, 18), (19, 21), (22, 25), (26, 28), (29, 32), (33, CONTRACT_DTE_MAX)]:
        try:
            req = GetOptionContractsRequest(
                underlying_symbols=[symbol], status="active",
                expiration_date_gte=(today_d + timedelta(days=start_dte)).isoformat(),
                expiration_date_lte=(today_d + timedelta(days=end_dte)).isoformat()
            )
            resp = trading_client.get_option_contracts(req)
            if hasattr(resp, "option_contracts"):
                all_contracts.extend(resp.option_contracts)
        except:
            pass
    if not all_contracts: return None

    candidates = []
    for c in all_contracts:
        try:
            strike = float(c.strike_price)
            if c.type != ("call" if direction == "bullish" else "put"): continue
            if direction == "bullish" and strike <= price: continue
            if direction == "bearish" and strike >= price: continue
            open_interest = getattr(c, "open_interest", 0) or 0
            if open_interest < MIN_OPTION_OI: continue
            bid, ask = _quote_option(data_client, c.symbol)
            if not bid or not ask: continue
            if ask - bid > MAX_OPTION_SPREAD: continue
            mid = (bid + ask) / 2
            if mid <= 0 or mid * 100 > budget: continue
            dte = (c.expiration_date - today_d).days
            otm_pct = (strike / price - 1) * 100 if direction == "bullish" else (1 - strike / price) * 100
            if dte < 15 and abs(otm_pct) > 5: continue
            candidates.append((c, mid, dte, otm_pct))
        except:
            pass

    if not candidates: return None
    candidates.sort(key=lambda x: (abs(x[3]), -x[2]))
    return candidates[0]


def _get_signal(llm, summary, watchlist):
    allocated = summary['equity'] * ALLOCATED_PCT
    per_pos_budget = allocated * PER_POSITION_PCT
    prompt = f"""You are an options trader. Analyze:
- Account: ${summary['equity']:.0f} total, ${allocated:.0f} allocated to options
- Max premium per position: ~${per_pos_budget:.0f}
- Watchlist ({len(watchlist)} stocks): {', '.join(watchlist)}
- Open options: {summary.get('open_options', 0)}
- SPY daily change: {summary.get('spy_pct', 'N/A')}%

Pick ONE symbol and direction. Respond JSON only:
{{"symbol": "SPY", "direction": "bullish|bearish|hold", "reasoning": "reason"}}"""
    try:
        content = llm.call(messages=[{"role": "user", "content": prompt}], max_tokens=200)
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            content = content[start:end+1]
        return json.loads(content)
    except Exception as e:
        logger.error(f"Signal failed: {e}")
        return {"symbol": None, "direction": "hold", "error": str(e)}


class OptionsBot:
    def __init__(self):
        self.alpaca = AlpacaClient()
        self.opt_data = OptionHistoricalDataClient(Config.ALPACA_API_KEY, Config.ALPACA_SECRET_KEY)
        self.llm = LLMEngine()
        self.notif = NotificationManager(provider=Config.NOTIFY_PROVIDER, config=Config.get_notification_config())
        self.starting_value = self.alpaca.get_portfolio_value()
        self.discovery = StockDiscovery()
        self.watchlist = UNIVERSE_100[:OPTIONS_WATCHLIST_SIZE]
        self.last_discovery = None
        self.cycle_count = 0
        self.status_interval = 4
        self.last_market_state = False
        self.last_summary_date = None
        self.day_start_value = self.starting_value
        self.start_date = datetime.now()
        self._daily_trades = 0
        self._daily_wins = 0
        self._daily_losses = 0
        logger.info(f"Options bot started. Account: ${self.starting_value:.2f}")

    def _discover_watchlist(self):
        now = datetime.now()
        if self.last_discovery and (now - self.last_discovery).seconds < 3600:
            return self.watchlist
        try:
            # Start with trending stocks (dynamic discovery) for fresh market signals
            trending = self.discovery.discover_trending_stocks()
            pool = list(dict.fromkeys(t for t in trending if t.upper() not in Config.BLACKLIST))
            # Fill remainder with core universe for stability (ensures 50-stock coverage)
            for s in UNIVERSE_100:
                if len(pool) >= OPTIONS_WATCHLIST_SIZE:
                    break
                if s not in pool and s.upper() not in Config.BLACKLIST:
                    pool.append(s)
            self.watchlist = pool[:OPTIONS_WATCHLIST_SIZE]
            self.last_discovery = now
            logger.info(f"Options watchlist refreshed: {len(self.watchlist)} stocks (trending+core universe)")
        except Exception as e:
            logger.warning(f"Watchlist refresh failed: {e}")
        return self.watchlist

    def _manage_positions(self):
        market_open = self.alpaca.get_market_status()
        if market_open != self.last_market_state:
            if market_open:
                self.day_start_value = self.alpaca.get_portfolio_value()
                self._daily_trades = 0
                self._daily_wins = 0
                self._daily_losses = 0
                logger.info("Market opened - daily tracking reset")
            else:
                self._send_daily_summary()
            self.last_market_state = market_open
        if not market_open:
            return [], 0
        try:
            positions = self.alpaca.get_positions()
            opt_positions = [p for p in positions if len(p.symbol) > 10]
            for pos in opt_positions:
                self._manage(pos)
            total_deployed = sum(float(p.avg_entry_price) * float(p.qty) * 100 for p in opt_positions)
            return opt_positions, total_deployed
        except Exception as e:
            logger.error(f"Manage positions error: {e}")
            return [], 0

    def run_cycle(self):
        if not self.alpaca.get_market_status():
            logger.info("Market closed - skipping"); return
        self.cycle_count += 1
        try:
            opt_positions, total_deployed = self._manage_positions()

            acct = self.alpaca.get_account()
            equity = float(acct.equity)
            cash = float(acct.cash)
            allocated = equity * ALLOCATED_PCT
            total_cap = allocated * TOTAL_DEPLOYED_PCT

            if total_deployed >= total_cap:
                logger.info(f"Total premium cap reached (${total_deployed:.0f}/${total_cap:.0f})")
                return

            spy_price = _underlying_price("SPY")
            spy_pct = None
            if spy_price:
                bars = self.alpaca.get_bars("SPY", days=2)
                if bars is not None and len(bars) > 1:
                    spy_pct = round((spy_price / float(bars['close'].iloc[0]) - 1) * 100, 2)

            per_pos_budget = allocated * PER_POSITION_PCT
            viable = [s for s in self.watchlist if _has_viable_option(self.alpaca.trading, self.opt_data, s, per_pos_budget)]
            if not viable:
                logger.info(f"No symbols with viable options ({len(self.watchlist)} checked, max spread: ${MAX_OPTION_SPREAD:.2f}, min OI: {MIN_OPTION_OI})")
                return
            if len(viable) < len(self.watchlist):
                logger.info(f"Filtered watchlist: {len(viable)}/{len(self.watchlist)} have viable options (spread ≤ ${MAX_OPTION_SPREAD:.2f}, OI ≥ {MIN_OPTION_OI})")

            summary = {"equity": equity, "cash": cash, "open_options": len(opt_positions), "spy_pct": spy_pct}
            signal = _get_signal(self.llm, summary, viable)
            if signal.get("error"):
                self.notif.send(f"Options signal error: {signal['error']}", priority="high")
            if signal.get("direction") == "hold":
                logger.info(f"Hold: {signal.get('reasoning', '')}")
                return

            budget = min(per_pos_budget, total_cap - total_deployed, cash)
            self._open(signal["symbol"], signal["direction"], budget)

            if self.cycle_count % self.status_interval == 0:
                self.notif.send(
                    f"Options heartbeat (cycle {self.cycle_count})\n"
                    f"Account: ${equity:.0f} | Deployed: ${total_deployed:.0f}/{total_cap:.0f}\n"
                    f"Positions: {len(opt_positions)} | Watchlist: {len(self.watchlist)}",
                    priority="low"
                )

        except Exception as e:
            logger.error(f"Cycle error: {e}", exc_info=True)
            self.notif.send(f"Options cycle error: {e}", priority="high")

    def _manage_loop(self):
        self._manage_positions()

    def _open(self, symbol, direction, budget):
        logger.info(f"Signal: {direction} {symbol}")
        result = _find_contract(self.alpaca.trading, self.opt_data, symbol, direction, budget)
        if not result:
            logger.info(f"No suitable contract for {symbol} {direction}")
            self.notif.send(f"No contract found: {direction} {symbol}", priority="low")
            return
        contract, premium, dte, _ = result
        contracts = max(1, int(budget / (premium * 100)))
        try:
            self.alpaca.trading.submit_order(MarketOrderRequest(
                symbol=contract.symbol, qty=contracts, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY
            ))
            total_cost = premium * 100 * contracts
            self._daily_trades += 1
            save_trade("options", symbol, "BUY", contracts, entry_price=premium, strategy=f"{direction}_{dte}dte")
            msg = f"Bought {contracts} {symbol} {contract.type} ${contract.strike_price:.0f} @ ${premium:.2f} ({dte}dte, ${total_cost:.0f} total)"
            logger.info(msg)
            self.notif.send(msg, priority="high")
        except Exception as e:
            logger.error(f"Order failed: {e}")
            self.notif.send(f"Order failed: {symbol} {direction} - {e}", priority="high")

    def _manage(self, pos):
        try:
            cp = float(pos.current_price)
            ep = float(pos.avg_entry_price)
            pnl = (cp / ep - 1) * 100
            dte = _option_dte(pos.symbol)
            stop = _get_dynamic_stop(dte)
            logger.info(f"{pos.symbol}: PnL {pnl:+.1f}% (dte={dte}, stop={stop:.0%})")
            if pnl >= TARGET_GAIN_PCT:
                self.alpaca.trading.close_position(pos.symbol)
                dollar_pnl = (cp - ep) * float(pos.qty) * 100
                save_trade("options", pos.symbol, "TP CLOSE", float(pos.qty), entry_price=ep, exit_price=cp, pnl_pct=pnl, pnl_dollars=dollar_pnl)
                self.notif.send(f"Closed {pos.symbol} at +{pnl:.0f}% gain", priority="high")
                self._daily_wins += 1
            elif _force_exit_near_expiry(dte):
                self.alpaca.trading.close_position(pos.symbol)
                dollar_pnl = (cp - ep) * float(pos.qty) * 100
                save_trade("options", pos.symbol, "FORCE EXIT", float(pos.qty), entry_price=ep, exit_price=cp, pnl_pct=pnl, pnl_dollars=dollar_pnl)
                self.notif.send(f"Force exit {pos.symbol} at {pnl:.0f}% (near expiry EOD)", priority="high")
                self._daily_losses += 1
            elif pnl <= stop:
                self.alpaca.trading.close_position(pos.symbol)
                dollar_pnl = (cp - ep) * float(pos.qty) * 100
                save_trade("options", pos.symbol, "STOP LOSS", float(pos.qty), entry_price=ep, exit_price=cp, pnl_pct=pnl, pnl_dollars=dollar_pnl)
                self.notif.send(f"Closed {pos.symbol} at {pnl:.0f}% loss (stop={stop:.0%})", priority="high")
                self._daily_losses += 1
        except Exception as e:
            logger.error(f"Manage failed: {e}")

    def _send_daily_summary(self):
        from datetime import date as date_type
        today = date_type.today()
        if self.last_summary_date == today:
            return
        self.last_summary_date = today
        try:
            current_value = self.alpaca.get_portfolio_value()
            daily_pnl = current_value - self.day_start_value
            total_return_pct = ((current_value / self.starting_value) - 1) * 100
            days_elapsed = (datetime.now() - self.start_date).days

            save_daily_snapshot("options", self.day_start_value, current_value, daily_pnl, self._daily_trades, self._daily_wins, self._daily_losses)

            if today.weekday() == 4:
                weekly = generate_weekly_summary()
                if weekly:
                    self.notif.send(weekly, priority="low")

            self.notif.send(
                f"OPTIONS DAILY SUMMARY - {today.strftime('%b %d')}\n"
                f"Account: ${current_value:.2f} (started ${self.starting_value:.0f})\n"
                f"Total Return: {total_return_pct:+.2f}%\n"
                f"Day P&L: ${daily_pnl:+.2f}\n"
                f"Trades: {self._daily_trades} (W:{self._daily_wins}/L:{self._daily_losses})",
                priority="low"
            )
        except Exception as e:
            logger.error(f"Options daily summary failed: {e}")

    def start(self):
        self.notif.send(f"Options bot started. Account: ${self.starting_value:.0f}", priority="low")
        schedule.every(15).minutes.do(self._manage_loop)
        schedule.every(60).minutes.do(self.run_cycle)
        self.run_cycle()
        while True:
            schedule.run_pending()
            time.sleep(1)


if __name__ == "__main__":
    bot = OptionsBot()
    try: bot.start()
    except KeyboardInterrupt: logger.info("Stopped")
