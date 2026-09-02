"""Pure thesis logic: schema validation, lifecycle transitions, and every
trading decision the executor makes. No I/O, no Alpaca, no LLM — everything
here is unit-tested in tests/test_v2_logic.py.

Lifecycle:
    active ──fill──> entered ──exit──> closed        (terminal)
    active ──TTL (close window)──> expired           (terminal)
    active ──not re-issued by research──> cancelled  (terminal)

Research creates `active` theses and sets flags on `entered` ones
(pending_close, revised invalidation/TTL). The executor performs every
transition that touches the market. Terminal theses leave active_theses.json;
their full snapshot lives in the terminal journal event.
"""
from datetime import date, datetime, timedelta, timezone

from trader_v2.options import OPTION_EXIT_REASONS

EXIT_REASONS = ("disaster_stop", "research_close", "invalidation",
                "trailing_stop", "ttl_expiry", "reconcile_missing") + OPTION_EXIT_REASONS

DIRECTIONS = ("bullish", "bearish")

MAX_ZONE_WIDTH_PCT = 0.15     # entry zone no wider than 15% of its low
MAX_ZONE_DRIFT_PCT = 0.15     # zone must lie within ±15% of last close
MAX_RISK_PCT = 0.05           # worst-case fill (zone high) -> invalidation, default cap
TTL_RANGE = (1, 10)
CONVICTION_RANGE = (1, 5)


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Construction & validation
# --------------------------------------------------------------------------

SCREEN_FIELDS = ("volume_ratio", "chg_5d_pct", "rsi_14")


def screen_snapshot(candidate: dict) -> dict:
    """The screen metrics the hard gates judge, lifted off a candidate row so
    they travel with the thesis into every journal snapshot (entry/exit
    attribution needs the numbers the analyst saw, not tonight's)."""
    return {k: (candidate or {}).get(k) for k in SCREEN_FIELDS}


def hard_gate_reason(direction: str, candidate: dict, *, min_volume_ratio: float,
                     max_move_5d_pct: float, max_rsi: float):
    """Deterministic entry disqualifiers from the lessons file, applied to a
    NEW thesis against its candidate screen row. Returns a reason string or
    None. A gate set to 0 is off; a metric missing from the row never blocks
    (no data is not a violation). Direction-aware: the 5-day move is judged on
    magnitude (an extended drop is as exhausted as an extended run), and the
    RSI cap mirrors to a floor for bearish theses."""
    c = candidate or {}
    vol = c.get("volume_ratio")
    if min_volume_ratio > 0 and vol is not None and vol < min_volume_ratio:
        return f"volume_ratio {vol:.2f} < {min_volume_ratio:g}"
    mv = c.get("chg_5d_pct")
    if max_move_5d_pct > 0 and mv is not None and abs(mv) > max_move_5d_pct:
        return f"5d move {mv:+.2f}% beyond ±{max_move_5d_pct:g}%"
    rsi = c.get("rsi_14")
    if max_rsi > 0 and rsi is not None:
        if direction == "bearish":
            floor = 100 - max_rsi
            if rsi < floor:
                return f"rsi {rsi:.1f} < {floor:g} (bearish floor)"
        elif rsi > max_rsi:
            return f"rsi {rsi:.1f} > {max_rsi:g}"
    return None


def build_thesis(raw: dict, today: date, screen: dict = None, sector: str = None) -> dict:
    """Canonical thesis dict from a validated raw LLM item. `screen` is the
    candidate's gate metrics at creation (see screen_snapshot); `sector` its
    GICS sector from the universe (None when unknown)."""
    return {
        "id": f"T{today.strftime('%Y%m%d')}-{raw['symbol'].upper()}",
        "symbol": raw["symbol"].upper(),
        "direction": raw.get("direction", "bullish"),
        "conviction": int(raw["conviction"]),
        "entry_zone": [float(raw["entry_zone_low"]), float(raw["entry_zone_high"])],
        "invalidation_price": float(raw["invalidation_price"]),
        "price_target": float(raw["price_target"]),   # advisory only — exits are trail/invalidation
        "catalyst": str(raw["catalyst"]),
        "catalyst_date": raw.get("catalyst_date") or None,   # ISO date or None
        # The analyst's requested expression. Bearish is always "option" (puts
        # are its only expression); bullish defaults to shares. The executor's
        # choose_instrument gate can still downgrade an option request.
        "requested_instrument": ("option" if raw.get("direction") == "bearish"
                                 else (raw.get("instrument") or "shares")),
        "ttl_days": int(raw["ttl_days"]),
        "reasoning": str(raw["reasoning"]),
        "screen": dict(screen) if screen else None,
        "sector": sector or None,
        "status": "active",
        "created": _now_iso(),
        "expires": (today + timedelta(days=int(raw["ttl_days"]))).isoformat(),
        # instrument is chosen at ENTRY by deterministic code, never by the LLM:
        # "shares" or "option"; option carries {contract, type, strike, expiry,
        # entry_delta, entry_premium}. multiplier is 100 for options.
        "instrument": None, "option": None, "multiplier": 1,
        "entered_at": None, "entry_price": None, "qty": None, "order_id": None,
        "hwm": None, "trailing": False, "pending_close": False,
        "revisions": [], "last_skip": None,
        "closed_at": None, "exit_price": None, "exit_reason": None,
        "pnl_pct": None, "pnl_dollars": None,
    }


def validate_new_thesis(raw: dict, last_close: float, candidate_symbols: set,
                        entered_symbols: set, blacklist: set = frozenset(),
                        crypto_suffixes: tuple = (), today: date = None,
                        opt_min_conviction: int = 4, max_risk_pct: float = MAX_RISK_PCT,
                        opt_enabled: bool = True) -> tuple:
    """Returns (error_string | None). Purely checks one raw thesis dict from
    the LLM against the guardrails; caller builds the thesis on success.

    Direction-aware: bullish geometry is invalidation < zone < target (long
    shares or calls); bearish inverts to target < zone < invalidation (long
    puts — the ONLY bearish expression, so bearish additionally requires
    conviction >= opt_min_conviction and options enabled).

    Risk cap: the worst-case fill (zone high for bullish, zone low for
    bearish) may sit at most max_risk_pct from the invalidation — the exits
    truncate winners at low single digits, so risk must match."""
    try:
        sym = str(raw["symbol"]).upper()
        direction = str(raw.get("direction", "bullish"))
        conviction = int(raw["conviction"])
        lo = float(raw["entry_zone_low"])
        hi = float(raw["entry_zone_high"])
        inval = float(raw["invalidation_price"])
        target = float(raw["price_target"])
        ttl = int(raw["ttl_days"])
    except (KeyError, TypeError, ValueError) as e:
        return f"malformed thesis: {e}"

    if direction not in DIRECTIONS:
        return f"{sym}: bad direction {direction!r}"
    instrument = raw.get("instrument") or "shares"
    if instrument not in ("shares", "option"):
        return f"{sym}: bad instrument {instrument!r}"
    if direction == "bearish" and raw.get("instrument") == "shares":
        return f"{sym}: bearish cannot be shares (long puts are its only expression)"
    if sym not in candidate_symbols:
        return f"{sym}: not in tonight's candidate set (hallucinated?)"
    if sym in blacklist:
        return f"{sym}: blacklisted"
    if any(sym.endswith(sfx) for sfx in crypto_suffixes):
        return f"{sym}: crypto not allowed"
    if sym in entered_symbols:
        return f"{sym}: already has an entered thesis"
    if not (CONVICTION_RANGE[0] <= conviction <= CONVICTION_RANGE[1]):
        return f"{sym}: conviction {conviction} out of range"
    if not (0 < lo < hi):
        return f"{sym}: bad entry zone [{lo}, {hi}]"
    if (hi - lo) > lo * MAX_ZONE_WIDTH_PCT:
        return f"{sym}: entry zone wider than {MAX_ZONE_WIDTH_PCT:.0%}"
    if last_close and last_close > 0:
        if hi < last_close * (1 - MAX_ZONE_DRIFT_PCT) or lo > last_close * (1 + MAX_ZONE_DRIFT_PCT):
            return f"{sym}: zone [{lo}, {hi}] beyond ±{MAX_ZONE_DRIFT_PCT:.0%} of close {last_close}"
    if not (TTL_RANGE[0] <= ttl <= TTL_RANGE[1]):
        return f"{sym}: ttl {ttl} outside {TTL_RANGE}"

    if direction == "bullish":
        if inval >= lo:
            return f"{sym}: invalidation {inval} must be below zone low {lo}"
        risk = (hi - inval) / hi
        if risk > max_risk_pct:
            return (f"{sym}: invalidation {inval} is {risk:.1%} below zone high {hi} — "
                    f"deeper than the {max_risk_pct:.0%} risk cap")
        if target <= hi:
            return f"{sym}: target {target} must exceed zone high {hi}"
    else:  # bearish: price falling is the thesis
        if inval <= hi:
            return f"{sym}: bearish invalidation {inval} must be above zone high {hi}"
        risk = (inval - lo) / lo
        if risk > max_risk_pct:
            return (f"{sym}: bearish invalidation {inval} is {risk:.1%} above zone low {lo} — "
                    f"further than the {max_risk_pct:.0%} risk cap")
        if target >= lo:
            return f"{sym}: bearish target {target} must be below zone low {lo}"
        if not opt_enabled:
            return f"{sym}: bearish needs options, which are disabled (puts are its only expression)"
        if conviction < opt_min_conviction:
            return f"{sym}: bearish needs conviction >= {opt_min_conviction} (puts are its only expression)"

    cat_date = raw.get("catalyst_date")
    if cat_date:
        try:
            cd = date.fromisoformat(str(cat_date))
        except ValueError:
            return f"{sym}: unparseable catalyst_date {cat_date!r}"
        if today is not None and not (today <= cd <= today + timedelta(days=ttl)):
            return f"{sym}: catalyst_date {cat_date} outside [today, today+{ttl}d]"
    return None


def select_theses(validated: list, capacity: int) -> list:
    """Dedupe by symbol (keep max conviction) and truncate to capacity by
    conviction desc (ties: input order, stable)."""
    by_sym = {}
    for t in validated:
        cur = by_sym.get(t["symbol"])
        if cur is None or t["conviction"] > cur["conviction"]:
            by_sym[t["symbol"]] = t
    ranked = sorted(by_sym.values(), key=lambda t: -t["conviction"])
    return ranked[:max(0, capacity)]


def select_with_sector_cap(validated: list, capacity: int, max_per_sector: int,
                           taken_sectors=()) -> tuple:
    """select_theses' dedupe + conviction ranking, then at most
    `max_per_sector` theses per GICS sector, counting sectors already held
    (`taken_sectors`, from entered theses). A thesis with unknown sector
    (None) is never capped. Returns (selected, dropped) where dropped is a
    list of (thesis, reason) for the caller to journal; capacity truncation
    is silent, as in select_theses."""
    if not max_per_sector or max_per_sector <= 0:
        return select_theses(validated, capacity), []
    from collections import Counter
    counts = Counter(s for s in taken_sectors if s)
    selected, dropped = [], []
    for t in select_theses(validated, len(validated)):
        sec = t.get("sector")
        if sec and counts[sec] >= max_per_sector:
            dropped.append((t, f"sector cap: {sec} already has {counts[sec]} (max {max_per_sector})"))
            continue
        if len(selected) >= max(0, capacity):
            break
        selected.append(t)
        if sec:
            counts[sec] += 1
    return selected, dropped


# --------------------------------------------------------------------------
# Executor decisions
# --------------------------------------------------------------------------

def zone_gap_pct(entry_zone, last_close):
    """Signed distance from `last_close` to the NEAREST edge of `entry_zone`,
    as a fraction of the close; 0.0 when the close is inside the zone. Negative
    = zone entirely below the close (fills only on a pullback), positive = zone
    entirely above (fills only on a breakout). Journalled on `thesis_created`
    so zone reachability is measurable across nights — the first live week
    minted 5/5 theses with the whole zone below the close, and the journal
    only showed the executor's daily `above_zone` skips. None when the close
    is unknown."""
    if not last_close or last_close <= 0 or not entry_zone:
        return None
    lo, hi = entry_zone
    if lo <= last_close <= hi:
        return 0.0
    edge = hi if last_close > hi else lo
    return round((edge - last_close) / last_close, 4)


def in_entry_zone(price: float, zone: list) -> bool:
    return zone[0] <= price <= zone[1]


def entry_skip_reason(price: float, zone: list):
    if price > zone[1]:
        return "above_zone"
    if price < zone[0]:
        return "below_zone"
    return None


def spendable_cash(cash: float, buying_power) -> float:
    """What a new order may actually spend: the smaller of the account's cash
    and its buying_power (the figure Alpaca's order gate checks). A missing or
    non-positive buying_power falls back to cash."""
    cash = float(cash or 0)
    try:
        bp = float(buying_power)
    except (TypeError, ValueError):
        return max(0.0, cash)
    if bp <= 0:
        return max(0.0, cash)
    return max(0.0, min(cash, bp))


def position_size(capital: float, max_positions: int, cap_pct: float,
                  cash: float, price: float, min_notional: float) -> float:
    """Fractional qty for a new entry; 0.0 means skip. Equal-weight slice of
    V2 capital, hard-capped at cap_pct of capital, clamped to available cash."""
    if price is None or price <= 0 or max_positions <= 0:
        return 0.0
    notional = capital / max_positions
    notional = min(notional, capital * cap_pct)
    notional = min(notional, cash * 0.98)
    if notional < min_notional:
        return 0.0
    return round(notional / price, 6)


def choose_instrument(t: dict, today: date, options_level: int, sleeve_room: float,
                      *, opt_enabled: bool, min_conviction: int, min_premium: float) -> tuple:
    """('shares' | 'option' | 'skip', reason | None) for an active thesis at
    entry time. Thesis-driven policy: the analyst requests the expression per
    thesis (requested_instrument); an option request must still clear the
    deterministic gates (conviction bar, account level, sleeve room). Contract
    viability (a real chain passing every filter) is checked separately by the
    executor — this is only the policy gate.

    Bearish theses have no shares fallback (no shorting): anything short of the
    option gates is 'skip', and the reason is journaled."""
    bearish = t.get("direction") == "bearish"
    fallback = ("skip", ) if bearish else ("shares", )

    def _no(reason):
        return fallback[0], reason

    # Bearish is inherently an option request; legacy theses without the field
    # (pre-schema records still in the book) default to shares.
    requested = t.get("requested_instrument") or ("option" if bearish else "shares")
    if requested != "option":
        return _no("not_requested")
    if not opt_enabled:
        return _no("options_disabled")
    if options_level < 2:
        return _no("options_level")
    if t["conviction"] < min_conviction:
        return _no("conviction_below_bar")
    if sleeve_room < min_premium:
        return _no("sleeve_full")
    return "option", None


def sleeve_room(theses: list, capital: float, sleeve_cap_pct: float) -> float:
    """Dollars of premium budget left in the options sleeve: cap minus the
    entry premium of every open option position."""
    open_premium = sum(
        (t.get("entry_price") or 0) * (t.get("qty") or 0) * t.get("multiplier", 1)
        for t in theses
        if t.get("status") == "entered" and t.get("instrument") == "option")
    return max(0.0, capital * sleeve_cap_pct - open_premium)


def update_trail(t: dict, price: float, activate_pct: float) -> bool:
    """Ratchet hwm (up only) and flip sticky `trailing` at entry*(1+activate).
    Returns True if state changed. Only meaningful for entered theses."""
    if t["status"] != "entered" or price is None or price <= 0:
        return False
    changed = False
    if t["hwm"] is None or price > t["hwm"]:
        t["hwm"] = price
        changed = True
    if not t["trailing"] and t["entry_price"] and t["hwm"] >= t["entry_price"] * (1 + activate_pct):
        t["trailing"] = True
        changed = True
    return changed


def exit_decision(t: dict, price: float, close_window: bool,
                  disaster_pct: float, trail_pct: float, today: date):
    """First matching exit reason for an entered thesis, or None.

    Precedence: disaster (every cycle) > research close > invalidation
    (close window ONLY — v1 lesson) > trailing (every cycle; realizes a locked
    gain) > TTL (close window)."""
    if t["status"] != "entered" or price is None or price <= 0:
        return None
    if t["entry_price"] and price <= t["entry_price"] * (1 + disaster_pct):
        return "disaster_stop"
    if t["pending_close"]:
        return "research_close"
    if close_window and price <= t["invalidation_price"]:
        return "invalidation"
    if t["trailing"] and t["hwm"] and price <= t["hwm"] * (1 + trail_pct):
        return "trailing_stop"
    if close_window and today >= date.fromisoformat(t["expires"]):
        return "ttl_expiry"
    return None


def daily_loss_halted(equity: float, last_equity: float, halt_pct: float) -> bool:
    if not last_equity or last_equity <= 0:
        return False
    return (equity - last_equity) / last_equity <= halt_pct


# --------------------------------------------------------------------------
# Transitions
# --------------------------------------------------------------------------

def apply_fill(t: dict, fill_price: float, qty: float, order_id, today: date = None,
               instrument: str = "shares", option_meta: dict = None) -> dict:
    """For options: fill_price is the PREMIUM per contract, qty is integer
    contracts, option_meta = {contract, type, strike, expiry, entry_delta}."""
    if t["status"] != "active":
        raise ValueError(f"cannot enter thesis {t['id']} in status {t['status']}")
    if instrument == "option" and not option_meta:
        raise ValueError(f"option fill for {t['id']} needs option_meta")
    today = today or datetime.now(timezone.utc).date()
    t.update(status="entered", entered_at=_now_iso(), entry_price=float(fill_price),
             qty=float(qty), order_id=str(order_id) if order_id else None,
             hwm=float(fill_price), trailing=False,
             instrument=instrument,
             option=dict(option_meta) if option_meta else None,
             multiplier=100 if instrument == "option" else 1,
             # TTL clock restarts at FILL: the pre-entry TTL decays unfilled
             # zones; once entered, the position gets its full ttl_days to play
             # out (CSW 7/21: written Fri, filled Mon, expired Tue = 1 day of
             # runway — a clock artifact, not a thesis judgment).
             expires=(today + timedelta(days=t["ttl_days"])).isoformat())
    return t


def apply_exit(t: dict, fill_price: float, reason: str) -> dict:
    if t["status"] != "entered":
        raise ValueError(f"cannot exit thesis {t['id']} in status {t['status']}")
    if reason not in EXIT_REASONS:
        raise ValueError(f"unknown exit reason {reason}")
    entry = t["entry_price"] or fill_price
    t.update(status="closed", closed_at=_now_iso(), exit_price=float(fill_price),
             exit_reason=reason,
             pnl_pct=round((fill_price / entry - 1) * 100, 4) if entry else None,
             pnl_dollars=round((fill_price - entry) * (t["qty"] or 0) * t.get("multiplier", 1), 4))
    return t


def broker_symbol(t: dict) -> str:
    """The symbol the BROKER knows this position by: the OSI contract for an
    option thesis, the plain equity symbol otherwise."""
    if t.get("instrument") == "option" and t.get("option"):
        return t["option"]["contract"]
    return t["symbol"]


def reconcile_classify(entered: list, broker_qty: dict) -> tuple:
    """Pure reconcile: match entered theses against broker positions by their
    broker symbol (OSI contract for options).

    Returns (missing, drifted, untracked):
      missing   — theses with no broker position (close as reconcile_missing)
      drifted   — (thesis, broker_qty) pairs where qty diverged (adopt broker)
      untracked — broker symbols (equity or OSI) no thesis accounts for
    """
    missing, drifted = [], []
    tracked = set()
    for t in entered:
        bsym = broker_symbol(t)
        tracked.add(bsym)
        if bsym not in broker_qty:
            missing.append(t)
        elif abs(broker_qty[bsym] - (t["qty"] or 0)) > 1e-6:
            drifted.append((t, broker_qty[bsym]))
    untracked = [s for s in broker_qty if s not in tracked]
    return missing, drifted, untracked


def apply_terminal(t: dict, status: str) -> dict:
    """active -> expired | cancelled."""
    if t["status"] != "active":
        raise ValueError(f"cannot mark {status}: thesis {t['id']} is {t['status']}")
    if status not in ("expired", "cancelled"):
        raise ValueError(f"bad terminal status {status}")
    t.update(status=status, closed_at=_now_iso())
    return t


def revision_risk_breach(t: dict, new_inval: float, max_risk_pct):
    """Reason string if moving an entered thesis's invalidation to new_inval
    would put the worst-case zone fill more than max_risk_pct from it (the
    same cap validation applies at creation), else None. Measured against the
    zone, not entry_price, so it also holds for option theses (whose
    entry_price is a premium). None cap = no check."""
    if max_risk_pct is None or not t.get("entry_zone"):
        return None
    lo, hi = t["entry_zone"]
    if t.get("direction", "bullish") == "bullish":
        risk = (hi - new_inval) / hi
        if risk > max_risk_pct:
            return f"invalidation {new_inval} is {risk:.1%} below zone high {hi} (cap {max_risk_pct:.0%})"
    else:
        risk = (new_inval - lo) / lo
        if risk > max_risk_pct:
            return f"invalidation {new_inval} is {risk:.1%} above zone low {lo} (cap {max_risk_pct:.0%})"
    return None


def apply_revision(t: dict, changes: dict, reasoning: str, today: date,
                   max_risk_pct: float = None) -> dict:
    """Research may revise invalidation_price and/or ttl_days on an ENTERED
    thesis. Records the old values in `revisions`. An invalidation that would
    breach the risk cap is refused and recorded under `rejected` instead."""
    if t["status"] != "entered":
        raise ValueError(f"cannot revise thesis {t['id']} in status {t['status']}")
    rec = {"ts": _now_iso(), "reasoning": reasoning, "changes": {}}
    if "invalidation_price" in changes and changes["invalidation_price"]:
        new_inval = float(changes["invalidation_price"])
        breach = revision_risk_breach(t, new_inval, max_risk_pct)
        if breach:
            rec["rejected"] = {"invalidation_price": [t["invalidation_price"], new_inval], "why": breach}
        else:
            rec["changes"]["invalidation_price"] = [t["invalidation_price"], new_inval]
            t["invalidation_price"] = new_inval
    if "ttl_days" in changes and changes["ttl_days"]:
        new_ttl = int(changes["ttl_days"])
        if TTL_RANGE[0] <= new_ttl <= TTL_RANGE[1]:
            rec["changes"]["ttl_days"] = [t["ttl_days"], new_ttl]
            t["ttl_days"] = new_ttl
            t["expires"] = (today + timedelta(days=new_ttl)).isoformat()
    if rec["changes"] or rec.get("rejected"):
        t["revisions"].append(rec)
    return t


# --------------------------------------------------------------------------
# Journal event builders (shapes asserted in tests)
# --------------------------------------------------------------------------

def event(kind: str, **payload) -> dict:
    return {"ts": _now_iso(), "event": kind, **payload}
