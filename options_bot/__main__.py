import logging
import os
import time
import json
import re
import csv
import schedule
from datetime import datetime, timedelta, date
import numpy as np
import pandas as pd
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest, GetOptionContractsRequest
from alpaca.data import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import OptionSnapshotRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from trader.config import Config
from trader.alpaca_client import AlpacaClient
from trader.llm_engine import LLMEngine, OPTIONS_SIGNAL_TOOL
from trader.notifications import NotificationManager as BaseNotif
from trader.stock_discovery import StockDiscovery, UNIVERSE_100
from trader.technical_analysis import TechnicalAnalysis
from trader.tracker import save_daily_snapshot, save_trade, generate_weekly_summary

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="[%H:%M:%S]")
logger = logging.getLogger("options")

ALLOCATED_PCT = 0.40           # 40% allocated (~$340 at current equity, ~$85/position)
PER_POSITION_PCT = 0.20        # 20% of allocated per position (~$66 at current equity, 2-3 positions)
TOTAL_DEPLOYED_PCT = 0.50      # 50% of allocated total cap
TARGET_GAIN_PCT = 50
CONTRACT_DTE_MIN = 7
CONTRACT_DTE_MAX = 35
OPTIONS_WATCHLIST_SIZE = 50
MAX_CONTRACTS_PER_POSITION = 3

MAX_OPTIONS_POSITIONS = 2
MIN_OPTION_OI = 100
MAX_OPTION_SPREAD = 0.50
MIN_CONTRACT_VALUE = 15
MIN_IV = 0.30


# Symbols excluded from options trading (hard block, not sent to LLM)
OPTIONS_BLACKLIST = [s.strip().upper() for s in os.getenv("OPTIONS_BLACKLIST", "").split(",") if s.strip()]

# Budget-aware blacklist tiers: as equity grows, more symbols become viable.
# $1,500 (current): full blacklist
# $5,000:          remove financials (JPM, BAC, V, MA, GS, AXP)
# $10,000:         remove most large-cap slow movers — ATM options affordable
# $25,000+:        ditch blacklist entirely, rely on liquidity filters only
BLACKLIST_TIER_5K = {"JPM", "BAC", "V", "MA", "GS", "AXP"}
BLACKLIST_TIER_10K = {
    "GILD","AMGN","BMY","PFE","JNJ","MRK","ABT","MDT","BSX",
    "LMT","RTX","NOC","GD","HII","TDG","HEI",
    "HD","LOW","TJX","ROST","YUM",
    "CMCSA","EA","ATVI",
    "HON","MMM","EMR","ROK","PH","FTV","ITW","IR","DOV","XYL","CARR","OTIS",
    "XOM","CVX","COP","EOG","PXD","PSX","BKR",
    "NEE","DUK","SO","D","AEP","EXC","ES","XEL","WEC","AWK","CMS","LNT","EVRG","PNW","OGE",
    "AMT","PLD","EQIX","CCI","SPG","O","WELL","AVB","EQR","MAA","UDR","EXR","PSA","IRM","DLR",
    "PG","KO","PEP","MDLZ","KHC","GIS","K","SJM","CAG","MKC","CLX","CL","CHD","ENR","SPB","HELE",
    "T","VZ","TMUS","SHEN","USM",
    "UNH","CI","ELV","MCK","ABC","CAH",
    "LUMN","CCOI","CVS","HUM","CNC","MOH",
    "PARA","WBD","PVH","RL","VFC",
    "TXN","MCHP","ADI","NXPI",
    "WMT","MS","BLK","C","WFC","ESRX",
}

def _get_effective_blacklist(equity):
    blacklist = set(OPTIONS_BLACKLIST)
    if equity >= 5000:
        blacklist -= BLACKLIST_TIER_5K
    if equity >= 10000:
        blacklist -= BLACKLIST_TIER_10K
    if equity >= 25000:
        blacklist = set()
    return blacklist

class NotificationManager(BaseNotif):
    def send(self, message, priority="normal"):
        msg = f"[OPTIONS] {message}"
        super().send(msg, priority)


def _underlying_price(symbol):
    try:
        stock_data = StockHistoricalDataClient(Config.ALPACA_API_KEY, Config.ALPACA_SECRET_KEY)
        bars = stock_data.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
            start=date.today() - timedelta(days=5)
        ))
        if not bars.df.empty:
            return float(bars.df["close"].iloc[-1])
    except Exception:
        logger.debug("_underlying_price failed for %s", symbol)
    return None


def _option_dte(symbol):
    try:
        date_str = symbol[-15:-9]
        exp = datetime.strptime(date_str, "%y%m%d").date()
        return (exp - date.today()).days
    except Exception:
        logger.debug("_option_dte failed for %s", symbol)
    return None

def _get_dynamic_stop(dte):
    if dte is None:
        return None
    if dte <= 5:
        return -25
    elif dte <= 14:
        return -40
    else:
        return -55

def _force_exit_near_expiry(dte):
    if dte is None:
        return False
    if dte <= 3:
        now = datetime.now()
        if now.hour >= 15:
            return True
    return False

def _batch_quote_options(data_client, contract_symbols):
    if not contract_symbols:
        return {}
    quotes = {}
    for i in range(0, len(contract_symbols), 100):
        batch = contract_symbols[i:i+100]
        try:
            req = OptionSnapshotRequest(symbol_or_symbols=batch)
            resp = data_client.get_option_snapshot(req)
            if isinstance(resp, dict):
                quotes.update(resp)
        except Exception as e:
            logger.debug("Batch quote failed for batch starting at %d: %s", i, e)
    return quotes

def _contract_is_otm(c, price):
    strike = float(c.strike_price)
    return (c.type == "call" and strike > price) or (c.type == "put" and strike < price)

def _get_snapshot_iv(snap):
    try:
        if isinstance(snap, dict):
            return float(snap.get("implied_volatility") or 0)
        return float(getattr(snap, "implied_volatility", 0) or 0)
    except Exception:
        logger.debug("_get_snapshot_iv failed")
    return 0.0

def _record_iv_snapshot(symbol, iv):
    path = f"{os.getenv('DATA_DIR', '/app/data')}/iv_history.csv"
    write_header = not os.path.exists(path)
    try:
        with open(path, "a", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(["date", "symbol", "iv"])
            writer.writerow([date.today().isoformat(), symbol, round(iv, 4)])
    except Exception as e:
        logger.warning(f"Failed to write IV history for {symbol}: {e}")

def _has_viable_option(trading_client, data_client, symbol, budget):
    today_d = date.today()
    price = _underlying_price(symbol)
    if not price:
        return False

    total_contracts = 0
    rejected = {"no_price": 0, "itm": 0, "low_oi": 0, "wide_spread": 0, "budget": 0, "otm_violation": 0, "low_iv": 0}
    otm_contracts = []

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
                total_contracts += 1
                if c.type not in ("call", "put"):
                    continue
                if not _contract_is_otm(c, price):
                    rejected["itm"] += 1
                    continue
                oi = int(getattr(c, "open_interest", 0) or 0)
                if oi < MIN_OPTION_OI:
                    rejected["low_oi"] += 1
                    continue
                otm_contracts.append(c)
        except Exception as e:
            logger.debug("Contract fetch failed for %s DTE range %d-%d: %s", symbol, start_dte, end_dte, e)

    if not otm_contracts:
        return False

    quotes = _batch_quote_options(data_client, [c.symbol for c in otm_contracts])

    for c in otm_contracts:
        try:
            snap = quotes.get(c.symbol)
            if not snap:
                rejected["no_price"] += 1
                continue
            if isinstance(snap, dict):
                q = snap.get("latest_quote") or snap.get("quote")
            else:
                q = getattr(snap, "latest_quote", None) or getattr(snap, "quote", None)
            if not q:
                rejected["no_price"] += 1
                continue
            if isinstance(q, dict):
                bid = float(q.get("bid_price") or q.get("bid") or 0)
                ask = float(q.get("ask_price") or q.get("ask") or 0)
            else:
                bid = float(getattr(q, "bid_price", 0) or 0)
                ask = float(getattr(q, "ask_price", 0) or 0)
            if not bid or not ask:
                rejected["no_price"] += 1
                continue
            if ask - bid > MAX_OPTION_SPREAD:
                rejected["wide_spread"] += 1
                continue
            mid = (bid + ask) / 2
            max_contract_cost = budget * 0.8
            if mid <= 0 or mid * 100 > max_contract_cost:
                rejected["budget"] += 1
                continue
            iv = _get_snapshot_iv(snap)
            if iv < MIN_IV:
                rejected["low_iv"] += 1
                continue
            _record_iv_snapshot(symbol, iv)
            dte = (c.expiration_date - today_d).days
            logger.debug(f"{symbol}: Found viable {c.type} ${float(c.strike_price):.0f} @ ${mid:.2f} ({dte} DTE, {oi} OI, IV={iv:.2f})")
            return True
        except Exception as e:
            logger.debug("Viable option check failed for %s contract %s: %s", symbol, c.symbol if hasattr(c, 'symbol') else '?', e)

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
        except Exception as e:
            logger.debug("Find contract fetch failed for %s DTE %d-%d: %s", symbol, start_dte, end_dte, e)
    if not all_contracts: return None

    candidates = []
    eligible = []
    for c in all_contracts:
        try:
            strike = float(c.strike_price)
            if c.type != ("call" if direction == "bullish" else "put"): continue
            if direction == "bullish" and strike <= price: continue
            if direction == "bearish" and strike >= price: continue
            open_interest = int(getattr(c, "open_interest", 0) or 0)
            if open_interest < MIN_OPTION_OI: continue
            eligible.append(c)
        except Exception as e:
            logger.debug("Find contract filter failed for %s: %s", symbol, e)

    if not eligible:
        return None

    quotes = _batch_quote_options(data_client, [c.symbol for c in eligible])

    for c in eligible:
        try:
            strike = float(c.strike_price)
            snap = quotes.get(c.symbol)
            if not snap:
                continue
            if isinstance(snap, dict):
                q = snap.get("latest_quote") or snap.get("quote")
            else:
                q = getattr(snap, "latest_quote", None) or getattr(snap, "quote", None)
            if not q:
                continue
            if isinstance(q, dict):
                bid = float(q.get("bid_price") or q.get("bid") or 0)
                ask = float(q.get("ask_price") or q.get("ask") or 0)
            else:
                bid = float(getattr(q, "bid_price", 0) or 0)
                ask = float(getattr(q, "ask_price", 0) or 0)
            if not bid or not ask: continue
            if ask - bid > MAX_OPTION_SPREAD: continue
            mid = (bid + ask) / 2
            max_contract_cost = budget * 0.8
            if mid <= 0 or mid * 100 > max_contract_cost: continue
            iv = _get_snapshot_iv(snap)
            if iv < MIN_IV: continue
            dte = (c.expiration_date - today_d).days
            otm_pct = (strike / price - 1) * 100 if direction == "bullish" else (1 - strike / price) * 100
            candidates.append((c, mid, dte, otm_pct, iv))
        except Exception as e:
            logger.debug("Find contract quote check failed for %s: %s", symbol, e)

    if not candidates: return None
    candidates.sort(key=lambda x: (abs(x[3]), -x[2]))
    _record_iv_snapshot(symbol, candidates[0][4])
    return candidates[0][:4]


def _get_signal(llm, summary, watchlist, ta_scores=None):
    ta_scores = ta_scores or {}
    allocated = summary['equity'] * ALLOCATED_PCT
    per_pos_budget = allocated * PER_POSITION_PCT

    # Per-symbol evidence from the TA already computed upstream. Without this the
    # model only saw a bare ticker list and could not justify any pick, so it held
    # every cycle. (RSI/MACD/momentum here are on the bot's intraday bars; the
    # daily-uptrend and IV gates were already enforced before these reached us.)
    lines = []
    for s in watchlist:
        ta = ta_scores.get(s, {})
        macd = ta.get("macd", {})
        trend = macd.get("trend") if isinstance(macd, dict) else None
        price = ta.get("current_price")
        rsi = ta.get("rsi_14")
        mom = ta.get("momentum_5")
        lines.append(
            f"  {s}: price=${price}, RSI={rsi}, mom5={mom}%, MACD={trend or 'n/a'}"
        )
    evidence = "\n".join(lines) if lines else "  (no TA available)"

    prompt = f"""You are an options trader specializing in momentum directionals (long calls/puts).

The candidates below have ALREADY PASSED the mechanical screens — confirmed daily
uptrend (EMA9 > EMA21 for 2+ of the last 5 days), implied volatility >= 0.30, and
liquid options (open interest, spread, and budget all OK). Do NOT re-verify these;
trust the screens. Your only job is to pick the SINGLE strongest momentum directional
setup, or hold if none is genuinely compelling.

Account: ${summary['equity']:.0f} total, ${allocated:.0f} to options, ~${per_pos_budget:.0f}/position
Open options: {summary.get('open_options', 0)} (max 2) | SPY today: {summary.get('spy_pct', 'N/A')}%

CANDIDATES (already trend- and IV-qualified):
{evidence}

HOW TO CHOOSE:
- These are all in confirmed daily uptrends, so a long CALL (bullish) is the default.
- Pick the name with the strongest, cleanest momentum (positive mom5, bullish MACD,
  RSI showing strength without being blown out).
- Choose bearish (put) only if a name's intraday momentum has clearly rolled over
  against the daily trend (negative mom5 + bearish MACD).
- If no candidate has a decisive momentum edge, hold — but with this many qualified
  names, a clear leader usually exists.

Pick ONE symbol and direction, or hold."""
    try:
        result = llm.call_structured(OPTIONS_SIGNAL_TOOL, messages=[{"role": "user", "content": [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]}], max_tokens=200)
        try:
            from trader.tracker import log_llm_call
            log_llm_call("options", getattr(llm, "last_model", None), "", prompt,
                         json.dumps(result), usage=getattr(llm, "last_usage", {}),
                         parse_ok=not result.get("error"))
        except Exception:
            pass
        return result
    except Exception as e:
        logger.error(f"Signal failed: {e}")
        return {"symbol": None, "direction": "hold", "error": str(e)}


class OptionsBot:
    def __init__(self):
        self.alpaca = AlpacaClient()
        self.opt_data = OptionHistoricalDataClient(Config.ALPACA_API_KEY, Config.ALPACA_SECRET_KEY)
        self.stock_data = StockHistoricalDataClient(Config.ALPACA_API_KEY, Config.ALPACA_SECRET_KEY)
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
        self._entry_times = {}
        self._hold_minutes = 15
        self._daily_trend_cache = {}
        logger.info(f"Options bot started. Account: ${self.starting_value:.2f}")

    def _check_daily_trend(self, symbol):
        now = datetime.now()
        cached = self._daily_trend_cache.get(symbol)
        if cached:
            passes, ts = cached
            if (now - ts).total_seconds() < 14400:
                return passes
        try:
            resp = self.stock_data.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                start=(date.today() - timedelta(days=45)).isoformat()
            ))
            if resp.df.empty:
                self._daily_trend_cache[symbol] = (False, now)
                return False
            df = resp.df
            if isinstance(df.index, pd.MultiIndex):
                close = df.xs(symbol, level=0)["close"]
            else:
                close = df["close"]
            if len(close) < 30:
                self._daily_trend_cache[symbol] = (False, now)
                return False
            ema9 = close.ewm(span=9, adjust=False).mean()
            ema21 = close.ewm(span=21, adjust=False).mean()
            delta = close.diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss.replace(0, np.nan)
            rsi14 = 100 - (100 / (1 + rs))
            last5 = pd.DataFrame({
                "ema9_gt_21": ema9 > ema21,
                "rsi_ok": (rsi14 >= 50) & (rsi14 <= 75)
            }).iloc[-5:]
            passes_bool = (last5["ema9_gt_21"] & last5["rsi_ok"]).sum() >= 2
            self._daily_trend_cache[symbol] = (passes_bool, now)
            logger.info(f"Daily trend {symbol}: {'pass' if passes_bool else 'fail'} ({passes_bool} of last 5 bars)")
            return passes_bool
        except Exception as e:
            logger.warning(f"Daily trend check failed for {symbol}: {e}")
            self._daily_trend_cache[symbol] = (False, now)
            return False

    def _discover_watchlist(self):
        now = datetime.now()
        if self.last_discovery and (now - self.last_discovery).seconds < 3600:
            return self.watchlist
        try:
            trending = self.discovery.discover_trending_stocks()
            pool = list(dict.fromkeys(t for t in trending if t.upper() not in Config.BLACKLIST))
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

            # Progressive funnel record — logged at whichever stage the cycle exits,
            # so we can see *why* no trade happened (best-effort, never blocks).
            cyc = {"bot": "options", "cycle": self.cycle_count,
                   "num_positions": len(opt_positions),
                   "total_deployed": round(total_deployed, 2)}

            def _emit(stage):
                try:
                    from trader.tracker import log_cycle
                    log_cycle({**cyc, "stage": stage})
                except Exception:
                    pass

            # Hard position limit: max 2 options positions at any time
            if len(opt_positions) >= MAX_OPTIONS_POSITIONS:
                logger.info(f"Position limit reached ({len(opt_positions)}/{MAX_OPTIONS_POSITIONS}) — skipping signal scan")
                _emit("position_limit")
                return

            acct = self.alpaca.get_account()
            equity = float(acct.equity)
            cash = float(acct.cash) * ALLOCATED_PCT
            allocated = equity * ALLOCATED_PCT
            total_cap = allocated * TOTAL_DEPLOYED_PCT
            cyc.update({"equity": round(equity, 2), "cash": round(cash, 2),
                        "total_cap": round(total_cap, 2)})

            if total_deployed >= total_cap:
                logger.info(f"Total premium cap reached (${total_deployed:.0f}/${total_cap:.0f})")
                _emit("total_cap")
                return

            spy_pct = None
            try:
                spy_bars = self.stock_data.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols="SPY", timeframe=TimeFrame.Day,
                    start=(date.today() - timedelta(days=10)).isoformat()
                ))
                if not spy_bars.df.empty:
                    closes = spy_bars.df["close"]
                    if isinstance(closes.index, pd.MultiIndex):
                        closes = closes.xs("SPY", level=0)
                    if len(closes) >= 2:
                        spy_pct = round((float(closes.iloc[-1]) / float(closes.iloc[-2]) - 1) * 100, 2)
            except Exception as e:
                logger.debug("SPY daily change failed: %s", e)

            per_pos_budget = allocated * PER_POSITION_PCT

            # Same-symbol lock: never open a new contract on an underlying we already hold.
            held_symbols = set()
            for p in opt_positions:
                m = re.match(r"^([A-Z]+)", p.symbol)
                if m:
                    held_symbols.add(m.group(1))

            effective_blacklist = _get_effective_blacklist(equity)
            logger.info(f"Options blacklist: {len(effective_blacklist)} blocked (equity=${equity:.0f}, tier={'full' if equity < 5000 else '5k' if equity < 10000 else '10k' if equity < 25000 else 'none'})")

            # Daily trend filter
            trend_passed = [s for s in self.watchlist if self._check_daily_trend(s)]
            logger.info(f"Daily trend filter: {len(trend_passed)}/{len(self.watchlist)} passed")
            cyc.update({"spy_pct": spy_pct, "watchlist": len(self.watchlist),
                        "trend_passed": len(trend_passed)})

            viable = [s for s in trend_passed if s not in held_symbols and s not in effective_blacklist and _has_viable_option(self.alpaca.trading, self.opt_data, s, per_pos_budget)]
            cyc["viable"] = len(viable)
            if not viable:
                logger.info(f"No symbols with viable options ({len(trend_passed)} trend-passed checked, spread≤${MAX_OPTION_SPREAD:.2f}, OI≥{MIN_OPTION_OI}, IV≥{MIN_IV})")
                _emit("no_viable")
                return
            if len(viable) < len(trend_passed):
                logger.info(f"Filtered watchlist: {len(viable)}/{len(trend_passed)} have viable options (spread≤${MAX_OPTION_SPREAD:.2f}, OI≥{MIN_OPTION_OI}, IV≥{MIN_IV})")

            # TA pre-filter: only send symbols with extreme RSI to the LLM.
            ta_scores = {}
            bars_df = self.alpaca.get_bars_batch(viable, days=7)
            if bars_df is not None:
                for s in viable:
                    try:
                        if s in bars_df.index.get_level_values('symbol'):
                            symbol_bars = bars_df.xs(s, level=0)
                            if len(symbol_bars) > 50:
                                ta_scores[s] = TechnicalAnalysis.compute_all(symbol_bars)
                    except Exception as e:
                        logger.debug("TA compute failed for %s: %s", s, e)

            viable_for_llm = [s for s in viable if s in ta_scores and (
                ta_scores[s]["rsi_14"] < 40 or ta_scores[s]["rsi_14"] > 60
            )]
            passed_ta = len(viable_for_llm)
            failed_ta = len(viable) - passed_ta
            if failed_ta > 0:
                logger.info(f"TA pre-filter: {passed_ta}/{len(viable)} pass RSI extreme threshold (kept: {viable_for_llm})")
            cyc["viable_for_llm"] = viable_for_llm
            if not viable_for_llm:
                logger.info("No symbols pass TA pre-filter (all RSI in neutral 40-60 range)")
                _emit("no_ta")
                return

            summary = {"equity": equity, "cash": cash, "open_options": len(opt_positions), "spy_pct": spy_pct}
            signal = _get_signal(self.llm, summary, viable_for_llm, ta_scores)
            cyc["signal"] = signal
            if signal.get("error"):
                self.notif.send(f"Options signal error: {signal['error']}", priority="high")
            if signal.get("direction") == "hold":
                logger.info(f"Hold: {signal.get('reasoning', '')}")
                _emit("hold")
                return

            budget = min(per_pos_budget, total_cap - total_deployed, cash)
            self._open(signal["symbol"], signal["direction"], budget, signal.get("reasoning", ""))
            _emit("trade")

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

    def _open(self, symbol, direction, budget, reasoning=""):
        logger.info(f"Signal: {direction} {symbol}")
        if reasoning:
            logger.info(f"Reasoning: {reasoning}")
        result = _find_contract(self.alpaca.trading, self.opt_data, symbol, direction, budget)
        if not result:
            logger.info(f"No suitable contract for {symbol} {direction}")
            self.notif.send(f"No contract found: {direction} {symbol}", priority="low")
            return
        contract, premium, dte, _ = result
        contracts = min(max(1, int(budget / (premium * 100))), MAX_CONTRACTS_PER_POSITION)
        try:
            self.alpaca.trading.submit_order(MarketOrderRequest(
                symbol=contract.symbol, qty=contracts, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY
            ))
            total_cost = premium * 100 * contracts
            self._daily_trades += 1
            self._entry_times[contract.symbol] = datetime.now()
            save_trade("options", symbol, "BUY", contracts, entry_price=premium, strategy=f"{direction}_{dte}dte", reason=reasoning)
            msg = f"Bought {contracts} {symbol} {contract.type} ${contract.strike_price:.0f} @ ${premium:.2f} ({dte}dte, ${total_cost:.0f} total)"
            if reasoning:
                msg += f"\nReason: {reasoning}"
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
            logger.info(f"{pos.symbol}: PnL {pnl:+.1f}% (dte={dte}, stop={stop})")
            if dte is None:
                self.alpaca.trading.close_position(pos.symbol)
                dollar_pnl = (cp - ep) * float(pos.qty) * 100
                save_trade("options", pos.symbol, "FORCE EXIT (NO DTE)", float(pos.qty), entry_price=ep, exit_price=cp, pnl_pct=pnl, pnl_dollars=dollar_pnl)
                self.notif.send(f"Force exit {pos.symbol} at {pnl:.0f}% (unparseable DTE)", priority="high")
                self._daily_losses += 1
                self._entry_times.pop(pos.symbol, None)
            elif pnl >= TARGET_GAIN_PCT:
                self.alpaca.trading.close_position(pos.symbol)
                dollar_pnl = (cp - ep) * float(pos.qty) * 100
                save_trade("options", pos.symbol, "TP CLOSE", float(pos.qty), entry_price=ep, exit_price=cp, pnl_pct=pnl, pnl_dollars=dollar_pnl)
                self.notif.send(f"Closed {pos.symbol} at +{pnl:.0f}% gain", priority="high")
                self._daily_wins += 1
                self._entry_times.pop(pos.symbol, None)
                return
            elif _force_exit_near_expiry(dte):
                self.alpaca.trading.close_position(pos.symbol)
                dollar_pnl = (cp - ep) * float(pos.qty) * 100
                save_trade("options", pos.symbol, "FORCE EXIT", float(pos.qty), entry_price=ep, exit_price=cp, pnl_pct=pnl, pnl_dollars=dollar_pnl)
                self.notif.send(f"Force exit {pos.symbol} at {pnl:.0f}% (near expiry EOD)", priority="high")
                self._daily_losses += 1
                self._entry_times.pop(pos.symbol, None)
                return
            if pnl <= stop:
                # Minimum hold time: don't stop-loss in the first N minutes.
                entered = self._entry_times.get(pos.symbol)
                held_minutes = (datetime.now() - entered).total_seconds() / 60 if entered else 999
                if held_minutes < self._hold_minutes:
                    logger.info(f"Holding {pos.symbol} ({pnl:+.1f}%): only {held_minutes:.0f}m old, skip stop (need ≥{self._hold_minutes}m)")
                    return
                self.alpaca.trading.close_position(pos.symbol)
                dollar_pnl = (cp - ep) * float(pos.qty) * 100
                save_trade("options", pos.symbol, "STOP LOSS", float(pos.qty), entry_price=ep, exit_price=cp, pnl_pct=pnl, pnl_dollars=dollar_pnl)
                self.notif.send(f"Closed {pos.symbol} at {pnl:.0f}% loss (stop={stop:.0%})", priority="high")
                self._daily_losses += 1
                self._entry_times.pop(pos.symbol, None)
                return

            # Minimum value floor
            contract_market_value = cp * float(pos.qty) * 100
            if contract_market_value < MIN_CONTRACT_VALUE:
                self.alpaca.trading.close_position(pos.symbol)
                dollar_pnl = (cp - ep) * float(pos.qty) * 100
                save_trade("options", pos.symbol, "MIN VALUE FLOOR", float(pos.qty), entry_price=ep, exit_price=cp, pnl_pct=pnl, pnl_dollars=dollar_pnl)
                self.notif.send(f"Force exit {pos.symbol} at ${contract_market_value:.0f} (below ${MIN_CONTRACT_VALUE} minimum value floor)", priority="high")
                self._daily_losses += 1
                self._entry_times.pop(pos.symbol, None)
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
