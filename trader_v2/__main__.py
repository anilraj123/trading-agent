"""trader_v2 entrypoint: schedule loop, market-close edge detection, daily
summary. Nightly research triggers off Alpaca's clock (open->closed edge +
startup catch-up), never wall-clock — container TZ is irrelevant.

CLI:
  python -m trader_v2                  # daemon
  python -m trader_v2 --cycle-once     # one executor cycle, exit
  python -m trader_v2 --research-now   # run nightly research once, exit
"""
import argparse
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import schedule

from trader.config import Config
from trader.alpaca_client import AlpacaClient
from trader.llm_engine import LLMEngine
from trader.notifications import NotificationManager as BaseNotif
from trader.stock_discovery import StockDiscovery
from trader.tracker import save_daily_snapshot, spy_buy_and_hold
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from . import executor, research, store
from .config import V2Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("trader_v2")

ET = ZoneInfo("America/New_York")


class NotificationManager(BaseNotif):
    def send(self, message, priority="normal"):
        super().send(f"[V2] {message}", priority)


class V2Bot:
    def __init__(self):
        V2Config.validate()
        self.alpaca = AlpacaClient()
        self.llm = LLMEngine()
        self.discovery = StockDiscovery()
        self.notif = NotificationManager(provider=Config.NOTIFY_PROVIDER,
                                         config=Config.get_notification_config())
        self.last_market_state = False
        self.cycle_count = 0
        if store.load_baseline() is None:
            equity = self.alpaca.get_portfolio_value()
            baseline = store.init_baseline(datetime.now(ET).date().isoformat(), equity)
            logger.info(f"Baseline initialized: {baseline}")

    # ------------------------------------------------------------------ cycle

    def cycle(self):
        self.cycle_count += 1
        try:
            clock = self.alpaca.get_clock()
        except Exception as e:
            logger.warning(f"clock fetch failed ({e}) — skipping cycle")
            return

        market_open = clock.is_open
        session_date = clock.timestamp.astimezone(ET).date()

        if market_open != self.last_market_state:
            if not market_open:
                logger.info("Market closed — running end-of-day jobs")
                self._on_market_close(session_date)
            else:
                logger.info("Market opened")
            self.last_market_state = market_open

        if not market_open:
            return
        try:
            executor.run_cycle(self.alpaca, self.notif, clock, self.cycle_count)
        except Exception as e:
            logger.error(f"executor cycle failed: {e}", exc_info=True)
            store.journal({"ts": datetime.now(timezone.utc).isoformat(),
                           "event": "error", "where": "cycle", "message": str(e)})

    # -------------------------------------------------------------- EOD chain

    def _on_market_close(self, session_date):
        run_state = store.load_run_state()
        if run_state.get("last_nightly_date") != session_date.isoformat():
            try:
                research.run_nightly(self.alpaca, self.llm, self.discovery, self.notif)
                run_state = store.load_run_state()
                run_state["last_nightly_date"] = session_date.isoformat()
                store.save_run_state(run_state)
            except Exception as e:
                logger.error(f"nightly research failed: {e}", exc_info=True)
                self.notif.send(f"nightly research FAILED: {e}", priority="high")
        if session_date.weekday() == 4 and run_state.get("last_weekly_date") != session_date.isoformat():
            try:
                research.run_weekly(self.llm, self.notif)
                run_state["last_weekly_date"] = session_date.isoformat()
                store.save_run_state(run_state)
            except Exception as e:
                logger.error(f"weekly review failed: {e}", exc_info=True)
        try:
            self._daily_summary(session_date)
        except Exception as e:
            logger.error(f"daily summary failed: {e}", exc_info=True)

    # ---------------------------------------------------------------- summary

    def _daily_summary(self, session_date):
        baseline = store.load_baseline()
        equity = self.alpaca.get_portfolio_value()
        pnl = equity - baseline["equity"]
        v2_value = V2Config.CAPITAL + pnl
        v2_return = pnl / V2Config.CAPITAL * 100

        spy_line = "SPY benchmark unavailable"
        alpha_line = ""
        try:
            start = datetime.fromisoformat(baseline["date"]).date()
            lookback = max(60, (datetime.now(ET).date() - start).days + 10)
            bars = self.alpaca.get_bars("SPY", days=lookback,
                                        timeframe=TimeFrame(1, TimeFrameUnit.Day))
            if bars is not None and len(bars) > 1:
                closes = {ts[1].date() if isinstance(ts, tuple) else ts.date(): float(c)
                          for ts, c in bars["close"].items()}
                spy_value, spy_total = spy_buy_and_hold([(start, V2Config.CAPITAL)], closes)
                if spy_total > 0:
                    spy_ret = (spy_value / spy_total - 1) * 100
                    alpha = v2_return - spy_ret
                    spy_line = f"SPY (same start): {spy_ret:+.2f}% → ${spy_value:.2f}"
                    alpha_line = f"Alpha: {alpha:+.2f} pp ({'ahead of' if alpha >= 0 else 'behind'} SPY)"
        except Exception as e:
            logger.warning(f"SPY benchmark failed: {e}")

        v1_line = ""
        try:
            import csv
            with open(V2Config.V1_HISTORY_FILE) as f:
                rows = [r for r in csv.DictReader(f) if r.get("bot") == "trading"]
            if rows:
                v1_line = f"v1 live equity: ${float(rows[-1]['end_value']):.2f}"
        except Exception:
            pass

        theses = store.load_theses()
        pos_lines = []
        for t in theses:
            if t["status"] == "entered":
                if t.get("instrument") == "option" and t.get("option"):
                    o = t["option"]
                    strike_s = f"{'C' if o['type'] == 'call' else 'P'}{o['strike']:g}"
                    pos_lines.append(f"{t['symbol']} {int(t['qty'])}x {strike_s} exp {o['expiry']}")
                else:
                    tag = " trailing" if t["trailing"] else ""
                    pos_lines.append(f"{t['symbol']} ({t['id']}{tag})")
        waiting = [t["symbol"] for t in theses if t["status"] == "active"]

        msg = (f"DAILY — {session_date.strftime('%b %d')}\n"
               f"V2 value: ${v2_value:.2f} on ${V2Config.CAPITAL:.0f} ({v2_return:+.2f}%)\n"
               f"{spy_line}\n{alpha_line}\n"
               + (f"{v1_line}\n" if v1_line else "")
               + f"Positions: {', '.join(pos_lines) or 'none'}\n"
               f"Waiting on zones: {', '.join(waiting) or 'none'}")
        self.notif.send(msg)
        save_daily_snapshot("v2", baseline["equity"], equity, pnl, 0, 0, 0)

    # ------------------------------------------------------------------ start

    def start(self):
        # Startup catch-up: if today was a session, it's past the close, and
        # the nightly hasn't run — run it now (covers restarts/deploys that
        # missed the open->closed edge).
        try:
            clock = self.alpaca.get_clock()
            session_date = clock.timestamp.astimezone(ET).date()
            run_state = store.load_run_state()
            holidays = self.alpaca.get_market_holidays(session_date, session_date)
            was_session = session_date.weekday() < 5 and session_date not in holidays
            # "Today's session already ended" == market closed AND the next open
            # is on a LATER date (if it's pre-market, next_open is today).
            # Handles early-close days with no hardcoded 16:00.
            session_over = (not clock.is_open
                            and clock.next_open.astimezone(ET).date() > session_date)
            if (was_session and session_over
                    and run_state.get("last_nightly_date") != session_date.isoformat()):
                logger.info("Startup catch-up: running missed end-of-day jobs")
                self._on_market_close(session_date)
            self.last_market_state = clock.is_open
        except Exception as e:
            logger.warning(f"startup catch-up skipped: {e}")

        self.notif.send(f"trader_v2 started (paper). Capital base ${V2Config.CAPITAL:.0f}, "
                        f"max {V2Config.MAX_POSITIONS} positions, "
                        f"trading {'ENABLED' if V2Config.TRADING_ENABLED else 'DISABLED (observe-only)'}",
                        priority="low")
        schedule.every(V2Config.CYCLE_MINUTES).minutes.do(self.cycle)
        self.cycle()
        while True:
            schedule.run_pending()
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycle-once", action="store_true")
    parser.add_argument("--research-now", action="store_true")
    args = parser.parse_args()

    bot = V2Bot()
    if args.research_now:
        research.run_nightly(bot.alpaca, bot.llm, bot.discovery, bot.notif)
    elif args.cycle_once:
        clock = bot.alpaca.get_clock()
        executor.run_cycle(bot.alpaca, bot.notif, clock, 0)
    else:
        bot.start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Stopped")
