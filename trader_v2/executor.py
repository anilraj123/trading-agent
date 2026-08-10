"""Intraday executor: the deterministic half of trader_v2. Runs every 15 min
during market hours. NO LLM anywhere in this file — it mechanically trades the
thesis plan produced by the nightly research job.

Cycle order (each step journaled):
  reconcile (always, even kill-switched) -> trail ratchet -> exits ->
  circuit breaker -> entries -> heartbeat + persist
"""
import logging
from datetime import date as date_type, datetime, timezone

from alpaca.trading.enums import OrderSide

from trader.risk_manager import in_close_window
from . import store
from . import thesis as th
from .config import V2Config

logger = logging.getLogger("trader_v2.executor")


def _price_of(alpaca, symbol):
    try:
        return alpaca.get_latest_price(symbol)
    except Exception as e:
        logger.warning(f"price fetch failed for {symbol}: {e}")
        return None


def _skip(t, reason, price):
    """Journal an entry skip, deduped to once per thesis+reason per day."""
    today = datetime.now(timezone.utc).date().isoformat()
    key = f"{today}:{reason}"
    if t.get("last_skip") == key:
        return
    t["last_skip"] = key
    store.journal(th.event("skip", thesis_id=t["id"], symbol=t["symbol"],
                           reason=reason, price=price))


def run_cycle(alpaca, notif, clock, cycle_count: int):
    """One executor cycle. `clock` is a fresh Alpaca clock (caller fetched it,
    fail-closed). Assumes market is open."""
    close_window = in_close_window(clock, V2Config.CLOSE_WINDOW_MIN)
    today = clock.timestamp.date() if hasattr(clock.timestamp, "date") else datetime.now(timezone.utc).date()
    kill_switch = not V2Config.TRADING_ENABLED

    theses = store.load_theses()
    run_state = store.load_run_state()

    # --- broker state -------------------------------------------------------
    try:
        positions = {p.symbol: p for p in alpaca.get_positions()}
        account = alpaca.get_account()
        equity, last_equity = float(account.equity), float(account.last_equity)
        cash = float(account.cash)
    except Exception as e:
        logger.error(f"broker state fetch failed: {e} — skipping cycle")
        store.journal(th.event("error", where="broker_state", message=str(e)))
        return

    entered = [t for t in theses if t["status"] == "entered"]
    actives = [t for t in theses if t["status"] == "active"]

    # --- 1. reconcile (always) ---------------------------------------------
    missing, drifted, untracked = [], [], []
    for t in entered:
        pos = positions.get(t["symbol"])
        if pos is None:
            price = _price_of(alpaca, t["symbol"]) or t["entry_price"]
            th.apply_exit(t, price, "reconcile_missing")
            store.journal(th.event("exit", thesis_id=t["id"], symbol=t["symbol"],
                                   qty=t["qty"], fill_price=price,
                                   entry_price=t["entry_price"], pnl_pct=t["pnl_pct"],
                                   pnl_dollars=t["pnl_dollars"], reason="reconcile_missing",
                                   thesis=t))
            notif.send(f"v2 reconcile: {t['symbol']} position gone from broker — thesis closed", priority="high")
            missing.append(t["symbol"])
        elif abs(float(pos.qty) - (t["qty"] or 0)) > 1e-6:
            drifted.append(t["symbol"])
            t["qty"] = float(pos.qty)  # adopt broker truth (partial fills etc.)
    tracked_syms = {t["symbol"] for t in entered if t["status"] == "entered"}
    for sym in positions:
        if sym not in tracked_syms:
            untracked.append(sym)
    if missing or drifted or untracked:
        store.journal(th.event("reconcile", missing=missing, qty_adjusted=drifted,
                               untracked=untracked))
        for sym in untracked:
            # dedupe via run_state (once per symbol per day)
            mark = f"untracked:{sym}:{today.isoformat()}"
            if run_state.get("last_untracked_notify") != mark:
                run_state["last_untracked_notify"] = mark
                notif.send(f"v2: untracked position {sym} in paper account — not touching it", priority="high")

    entered = [t for t in theses if t["status"] == "entered"]  # refresh after reconcile

    # --- 2 & 3. trail ratchet + exits ---------------------------------------
    prices = {t["symbol"]: _price_of(alpaca, t["symbol"]) for t in entered}
    for t in entered:
        price = prices.get(t["symbol"])
        th.update_trail(t, price, V2Config.TRAIL_ACTIVATE_PCT)
        reason = th.exit_decision(t, price, close_window, V2Config.DISASTER_STOP_PCT,
                                  V2Config.TRAIL_STOP_PCT, today)
        if not reason:
            continue
        if kill_switch:
            _skip(t, "halted", price)
            continue
        try:
            alpaca.close_position(t["symbol"])
            th.apply_exit(t, price, reason)
            store.save_theses(theses)
            store.journal(th.event("exit", thesis_id=t["id"], symbol=t["symbol"],
                                   qty=t["qty"], fill_price=price,
                                   entry_price=t["entry_price"], pnl_pct=t["pnl_pct"],
                                   pnl_dollars=t["pnl_dollars"], reason=reason, thesis=t))
            notif.send(f"v2 EXIT {t['symbol']} [{reason}] {t['pnl_pct']:+.2f}% (${t['pnl_dollars']:+.2f})")
        except Exception as e:
            logger.error(f"exit failed for {t['symbol']}: {e}")
            store.journal(th.event("error", where=f"exit:{t['symbol']}", message=str(e)))

    # --- 4. circuit breaker -------------------------------------------------
    halted = run_state.get("halted_date") == today.isoformat()
    if not halted and th.daily_loss_halted(equity, last_equity, V2Config.DAILY_LOSS_HALT_PCT):
        halted = True
        run_state["halted_date"] = today.isoformat()
        store.journal(th.event("circuit_breaker", equity=equity, last_equity=last_equity,
                               daily_pnl_pct=round((equity / last_equity - 1) * 100, 2)))
        notif.send(f"v2 CIRCUIT BREAKER: day P&L {(equity/last_equity-1)*100:+.1f}% — no new entries today",
                   priority="high")

    # --- 5. entries ---------------------------------------------------------
    if not halted and not kill_switch:
        entered_count = len([t for t in theses if t["status"] == "entered"])
        held_syms = {t["symbol"] for t in theses if t["status"] == "entered"} | set(positions)
        avail_cash = cash
        for t in sorted(actives, key=lambda x: -x["conviction"]):
            if t["status"] != "active":
                continue
            if entered_count >= V2Config.MAX_POSITIONS:
                _skip(t, "max_positions", None)
                continue
            if t["symbol"] in held_syms:
                _skip(t, "already_held", None)
                continue
            if t.get("direction") == "bearish":
                # Puts are the only bearish expression and the options entry
                # path lands in PR3 — until then bearish theses wait unfilled.
                _skip(t, "options_not_enabled", None)
                continue
            price = _price_of(alpaca, t["symbol"])
            if price is None:
                _skip(t, "no_price", None)
                continue
            zone_reason = th.entry_skip_reason(price, t["entry_zone"])
            if zone_reason:
                _skip(t, zone_reason, price)
                continue
            qty = th.position_size(V2Config.CAPITAL, V2Config.MAX_POSITIONS,
                                   V2Config.POSITION_CAP_PCT, avail_cash, price,
                                   V2Config.MIN_NOTIONAL)
            if qty <= 0:
                _skip(t, "no_cash", price)
                continue
            try:
                order = alpaca.submit_market_order(t["symbol"], OrderSide.BUY, qty)
                fill = float(getattr(order, "filled_avg_price", 0) or 0) or price
                th.apply_fill(t, fill, qty, getattr(order, "id", None))
                store.save_theses(theses)   # persist immediately after each fill
                store.journal(th.event("entry", thesis_id=t["id"], symbol=t["symbol"],
                                       qty=qty, requested_notional=round(qty * price, 2),
                                       fill_price=fill, order_id=str(getattr(order, "id", ""))))
                notif.send(f"v2 ENTRY {t['symbol']} {qty} @ ${fill:.2f} (thesis {t['id']}, conviction {t['conviction']})")
                entered_count += 1
                held_syms.add(t["symbol"])
                avail_cash -= qty * fill
            except Exception as e:
                logger.error(f"entry failed for {t['symbol']}: {e}")
                store.journal(th.event("error", where=f"entry:{t['symbol']}", message=str(e)))
    elif actives:
        for t in actives:
            if t["status"] == "active":
                _skip(t, "halted", None)

    # --- 6. unfilled TTL expiry (close window) ------------------------------
    if close_window:
        for t in theses:
            if t["status"] == "active" and today >= date_type.fromisoformat(t["expires"]):
                th.apply_terminal(t, "expired")
                store.journal(th.event("thesis_expired", thesis=t))

    # --- 7. heartbeat + persist ---------------------------------------------
    store.journal(th.event(
        "heartbeat", cycle=cycle_count, equity=equity, cash=round(cash, 2),
        entered=sorted({t["symbol"] for t in theses if t["status"] == "entered"}),
        waiting=len([t for t in theses if t["status"] == "active"]),
        close_window=close_window, halted=halted, kill_switch=kill_switch))
    store.save_theses(theses)
    store.save_run_state(run_state)
