"""Intraday executor: the deterministic half of trader_v2. Runs every 15 min
during market hours. NO LLM anywhere in this file — it mechanically trades the
thesis plan produced by the nightly research job.

Cycle order (each step journaled):
  reconcile (always, even kill-switched) -> trail ratchet -> exits ->
  circuit breaker -> entries -> heartbeat + persist

Instruments: shares are the default expression; an option (long call/put) is
entered only when the choose_instrument policy gate AND a viable contract line
up. Options never trade at market — limit at tick-rounded mid, escalating to a
marketable limit on exits.
"""
import logging
import time
from datetime import date as date_type, datetime, timezone

from alpaca.trading.enums import OrderSide

from trader.risk_manager import in_close_window
from . import guards
from . import options as opt
from . import options_data
from . import store
from . import thesis as th
from .config import V2Config

logger = logging.getLogger("trader_v2.executor")

STOP_TIERS = opt.parse_stop_tiers(V2Config.OPT_STOP_TIERS_SPEC)


def _price_of(alpaca, symbol):
    try:
        return alpaca.get_latest_price(symbol)
    except Exception as e:
        logger.warning(f"price fetch failed for {symbol}: {e}")
        return None


def _premium_of(alpaca, contract):
    """Current premium mid for one OSI contract, or None."""
    try:
        snaps = alpaca.get_option_snapshots([contract], feed=V2Config.OPT_FEED)
        snap = snaps.get(contract)
        if snap is None:
            return None
        bid, ask = options_data._quote_fields(snap)
        return opt.mid_price(bid, ask)
    except Exception as e:
        logger.warning(f"premium fetch failed for {contract}: {e}")
        return None


def _wait_for_fill(alpaca, order_id, wait_sec):
    """Poll an order until COMPLETELY filled or timeout. Returns
    (filled_qty, avg_price) — at timeout, whatever partial qty filled.
    Callers must cancel the remainder when filled_qty < requested."""
    deadline = time.monotonic() + wait_sec
    while time.monotonic() < deadline:
        try:
            o = alpaca.get_order(order_id)
            if str(getattr(o, "status", "")).lower().endswith("filled") and \
                    not str(getattr(o, "status", "")).lower().startswith("partial"):
                return float(o.filled_qty), float(o.filled_avg_price or 0) or None
        except Exception as e:
            logger.warning(f"order poll {order_id} failed: {e}")
        time.sleep(5)
    try:  # final look before giving up
        o = alpaca.get_order(order_id)
        filled = float(getattr(o, "filled_qty", 0) or 0)
        return filled, float(getattr(o, "filled_avg_price", 0) or 0) or None
    except Exception:
        return 0.0, None


def _close_option(alpaca, t, premium_mid, urgent):
    """Sell an option position. Non-urgent: limit at mid first (capture half
    the spread), then escalate. Urgent exits (stops/expiry) skip straight to
    the escalation. Escalation = close_position (cancels resting orders, then
    markets out) — an exit is never left dangling across cycles. Returns the
    realized premium, falling back to the quote mid."""
    contract, qty = t["option"]["contract"], int(t["qty"])
    if not urgent and premium_mid:
        limit = opt.tick_round(premium_mid, up=False)
        order = alpaca.submit_option_limit_order(contract, OrderSide.SELL, qty, limit)
        filled, avg = _wait_for_fill(alpaca, order.id, V2Config.OPT_FILL_WAIT_SEC)
        if filled >= qty:
            return avg or premium_mid
        alpaca.cancel_order(order.id)
    alpaca.close_position(contract)
    return premium_mid or t["entry_price"]


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

    # PDT guard is armed only on margin accounts (see guards.py); Alpaca
    # reports daytrade_count=null on cash accounts.
    pdt_armed = float(getattr(account, "multiplier", 1) or 1) > 1
    daytrade_count = getattr(account, "daytrade_count", None) if pdt_armed else None
    daytrade_count = int(daytrade_count) if daytrade_count is not None else None

    # Options-level downgrade detection: entries gate on the live level every
    # cycle anyway (choose_instrument); this exists to NOTIFY the change once.
    level_now = int(getattr(account, "options_trading_level", 0) or 0)
    if run_state.get("options_level") is not None and run_state["options_level"] != level_now:
        notif.send(f"v2: options trading level changed {run_state['options_level']} -> {level_now}"
                   + (" — options entries stop" if level_now < 2 else ""), priority="high")
        store.journal(th.event("options_level_changed",
                               old=run_state["options_level"], new=level_now))
    run_state["options_level"] = level_now

    # --- 1. reconcile (always) ---------------------------------------------
    # Matched by BROKER symbol: the OSI contract for option theses, the plain
    # equity symbol otherwise — so option positions are tracked, not spam.
    broker_qty = {sym: float(p.qty) for sym, p in positions.items()}
    miss, drift, untracked = th.reconcile_classify(entered, broker_qty)
    missing, drifted = [], []
    for t in miss:
        if t["instrument"] == "option":
            price = _premium_of(alpaca, t["option"]["contract"]) or t["entry_price"]
        else:
            price = _price_of(alpaca, t["symbol"]) or t["entry_price"]
        th.apply_exit(t, price, "reconcile_missing")
        store.journal(th.event("exit", thesis_id=t["id"], symbol=t["symbol"],
                               qty=t["qty"], fill_price=price,
                               entry_price=t["entry_price"], pnl_pct=t["pnl_pct"],
                               pnl_dollars=t["pnl_dollars"], reason="reconcile_missing",
                               thesis=t))
        notif.send(f"v2 reconcile: {th.broker_symbol(t)} position gone from broker — thesis closed", priority="high")
        missing.append(t["symbol"])
    for t, bqty in drift:
        drifted.append(t["symbol"])
        t["qty"] = bqty  # adopt broker truth (partial fills etc.)
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
    URGENT_OPTION_EXITS = ("premium_stop", "expiry_force_exit", "invalidation")
    for t in entered:
        price = prices.get(t["symbol"])
        if t["instrument"] == "option":
            # Premium drives stops/TP; the underlying drives invalidation.
            # No premium trailing — hwm on bid/ask mid whipsaws (hard TP instead).
            contract = t["option"]["contract"]
            premium = _premium_of(alpaca, contract)
            dte_val = opt.dte(date_type.fromisoformat(t["option"]["expiry"]), today)
            reason = opt.option_exit_decision(
                t, premium, price, dte_val, close_window, today,
                force_exit_dte=V2Config.OPT_FORCE_EXIT_DTE, stop_tiers=STOP_TIERS,
                take_profit_pct=V2Config.OPT_TAKE_PROFIT_PCT)
        else:
            th.update_trail(t, price, V2Config.TRAIL_ACTIVATE_PCT)
            reason = th.exit_decision(t, price, close_window, V2Config.DISASTER_STOP_PCT,
                                      V2Config.TRAIL_STOP_PCT, today)
        if not reason:
            continue
        if kill_switch:
            _skip(t, "halted", price)
            continue
        if pdt_armed and guards.pdt_exit_blocked(
                (t.get("entered_at") or "")[:10], today.isoformat(),
                daytrade_count, reason, V2Config.PDT_SOFT_MAX):
            _skip(t, f"pdt_deferred:{reason}", price)
            if reason in guards.CRITICAL_EXITS:
                notif.send(f"v2 PDT: {reason} on {t['symbol']} DEFERRED to next session "
                           f"(daytrade_count={daytrade_count}) — check manually", priority="high")
            continue
        try:
            if t["instrument"] == "option":
                fill = _close_option(alpaca, t, premium, urgent=reason in URGENT_OPTION_EXITS)
            else:
                alpaca.close_position(t["symbol"])
                fill = price
            th.apply_exit(t, fill, reason)
            store.save_theses(theses)
            store.journal(th.event("exit", thesis_id=t["id"], symbol=t["symbol"],
                                   instrument=t["instrument"], qty=t["qty"], fill_price=fill,
                                   entry_price=t["entry_price"], pnl_pct=t["pnl_pct"],
                                   pnl_dollars=t["pnl_dollars"], reason=reason, thesis=t))
            label = th.broker_symbol(t) if t["instrument"] == "option" else t["symbol"]
            notif.send(f"v2 EXIT {label} [{reason}] {t['pnl_pct']:+.2f}% (${t['pnl_dollars']:+.2f})")
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
    # capital anchor: static V2_CAPITAL (paper apples-to-apples) or live equity
    capital = V2Config.capital_base(equity)
    options_level = level_now
    if not halted and not kill_switch:
        entered_count = len([t for t in theses if t["status"] == "entered"])
        held_syms = {t["symbol"] for t in theses if t["status"] == "entered"} | \
                    {opt.parse_osi(s)["underlying"] if opt.parse_osi(s) else s for s in positions}
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
            price = _price_of(alpaca, t["symbol"])
            if price is None:
                _skip(t, "no_price", None)
                continue
            zone_reason = th.entry_skip_reason(price, t["entry_zone"])
            if zone_reason:
                _skip(t, zone_reason, price)
                continue

            # instrument policy gate, then contract viability
            sleeve = th.sleeve_room(theses, capital, V2Config.OPT_SLEEVE_CAP_PCT)
            if pdt_armed and guards.pdt_entry_blocked(daytrade_count):
                # An entry we might be unable to stop out same-day is an
                # entry we don't make (margin accounts only).
                _skip(t, "pdt_entry_blocked", price)
                continue
            instrument, gate_reason = th.choose_instrument(
                t, today, options_level, sleeve,
                opt_enabled=V2Config.OPT_ENABLED,
                min_conviction=V2Config.OPT_MIN_CONVICTION,
                min_premium=V2Config.OPT_MIN_PREMIUM)
            fallback_reason = gate_reason
            picked = None
            if instrument == "option":
                side = "call" if t["direction"] == "bullish" else "put"
                budget = min(capital * V2Config.OPT_MAX_PREMIUM_PCT, sleeve)
                try:
                    rows = options_data.fetch_chain_rows(
                        alpaca, t["symbol"], side, V2Config.OPT_DTE_MIN,
                        V2Config.OPT_DTE_MAX, today, spot=price, feed=V2Config.OPT_FEED)
                except Exception as e:
                    logger.warning(f"chain fetch failed for {t['symbol']}: {e}")
                    rows = []
                picked, why = opt.select_contract(
                    rows, side,
                    delta_min=V2Config.OPT_DELTA_MIN, delta_max=V2Config.OPT_DELTA_MAX,
                    dte_min=V2Config.OPT_DTE_MIN, dte_max=V2Config.OPT_DTE_MAX,
                    min_oi=V2Config.OPT_MIN_OI, max_spread_pct=V2Config.OPT_MAX_SPREAD_PCT,
                    min_premium=V2Config.OPT_MIN_PREMIUM, max_premium=budget)
                if picked is not None and opt.contracts_for_budget(picked["mid"], budget, avail_cash) < 1:
                    picked, why = None, "no_cash"
                if picked is None:
                    instrument, fallback_reason = ("skip" if t["direction"] == "bearish"
                                                   else "shares"), f"no_contract:{why}"

            if instrument == "skip":
                # Bearish with no viable put: nothing to trade (no shorting).
                _skip(t, f"no_bearish_expression:{fallback_reason}", price)
                continue

            try:
                if instrument == "option":
                    n = opt.contracts_for_budget(picked["mid"], budget, avail_cash)
                    limit = opt.tick_round(picked["mid"], up=False)
                    order = alpaca.submit_option_limit_order(picked["symbol"], OrderSide.BUY, n, limit)
                    filled, avg = _wait_for_fill(alpaca, order.id, V2Config.OPT_FILL_WAIT_SEC)
                    if filled < n:
                        alpaca.cancel_order(order.id)   # drop the unfilled remainder
                    if filled < 1:
                        _skip(t, "option_unfilled", picked["mid"])
                        continue   # retry next cycle with a fresh quote
                    fill = avg or picked["mid"]
                    th.apply_fill(t, fill, filled, order.id, instrument="option",
                                  option_meta={"contract": picked["symbol"], "type": picked["type"],
                                               "strike": picked["strike"],
                                               "expiry": picked["expiry"].isoformat(),
                                               "entry_delta": picked["delta"]})
                    store.save_theses(theses)
                    store.journal(th.event("entry", thesis_id=t["id"], symbol=t["symbol"],
                                           instrument="option", contract=picked["symbol"],
                                           qty=filled, fill_price=fill,
                                           premium_notional=round(fill * 100 * filled, 2),
                                           delta=picked["delta"], dte=picked["dte"],
                                           order_id=str(order.id)))
                    strike_s = f"{'C' if picked['type']=='call' else 'P'}{picked['strike']:g}"
                    notif.send(f"v2 ENTRY {t['symbol']} {int(filled)}x {strike_s} "
                               f"{picked['expiry'].strftime('%m/%d')} @ ${fill:.2f} "
                               f"(Δ{abs(picked['delta']):.2f}, {picked['dte']} DTE, conviction {t['conviction']})")
                    avail_cash -= fill * 100 * filled
                else:
                    qty = th.position_size(capital, V2Config.MAX_POSITIONS,
                                           V2Config.POSITION_CAP_PCT, avail_cash, price,
                                           V2Config.MIN_NOTIONAL)
                    if qty <= 0:
                        _skip(t, "no_cash", price)
                        continue
                    order = alpaca.submit_market_order(t["symbol"], OrderSide.BUY, qty)
                    fill = float(getattr(order, "filled_avg_price", 0) or 0) or price
                    th.apply_fill(t, fill, qty, getattr(order, "id", None))
                    store.save_theses(theses)   # persist immediately after each fill
                    store.journal(th.event("entry", thesis_id=t["id"], symbol=t["symbol"],
                                           instrument="shares", qty=qty,
                                           requested_notional=round(qty * price, 2),
                                           fill_price=fill, fallback_reason=fallback_reason,
                                           order_id=str(getattr(order, "id", ""))))
                    notif.send(f"v2 ENTRY {t['symbol']} {qty} @ ${fill:.2f} (thesis {t['id']}, conviction {t['conviction']})")
                    avail_cash -= qty * fill
                entered_count += 1
                held_syms.add(t["symbol"])
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
