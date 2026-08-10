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
MAX_INVALIDATION_DEPTH = 0.15 # invalidation no deeper than 15% below zone low
TTL_RANGE = (1, 10)
CONVICTION_RANGE = (1, 5)


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Construction & validation
# --------------------------------------------------------------------------

def build_thesis(raw: dict, today: date) -> dict:
    """Canonical thesis dict from a validated raw LLM item."""
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
        "ttl_days": int(raw["ttl_days"]),
        "reasoning": str(raw["reasoning"]),
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
                        opt_min_conviction: int = 4) -> tuple:
    """Returns (error_string | None). Purely checks one raw thesis dict from
    the LLM against the guardrails; caller builds the thesis on success.

    Direction-aware: bullish geometry is invalidation < zone < target (long
    shares or calls); bearish inverts to target < zone < invalidation (long
    puts — the ONLY bearish expression, so bearish additionally requires a
    dated catalyst and conviction >= opt_min_conviction)."""
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
        if inval < lo * (1 - MAX_INVALIDATION_DEPTH):
            return f"{sym}: invalidation {inval} deeper than {MAX_INVALIDATION_DEPTH:.0%} below zone"
        if target <= hi:
            return f"{sym}: target {target} must exceed zone high {hi}"
    else:  # bearish: price falling is the thesis
        if inval <= hi:
            return f"{sym}: bearish invalidation {inval} must be above zone high {hi}"
        if inval > hi * (1 + MAX_INVALIDATION_DEPTH):
            return f"{sym}: bearish invalidation {inval} further than {MAX_INVALIDATION_DEPTH:.0%} above zone"
        if target >= lo:
            return f"{sym}: bearish target {target} must be below zone low {lo}"
        if conviction < opt_min_conviction:
            return f"{sym}: bearish needs conviction >= {opt_min_conviction} (puts are its only expression)"
        if not raw.get("catalyst_date"):
            return f"{sym}: bearish needs a dated catalyst (puts are its only expression)"

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


# --------------------------------------------------------------------------
# Executor decisions
# --------------------------------------------------------------------------

def in_entry_zone(price: float, zone: list) -> bool:
    return zone[0] <= price <= zone[1]


def entry_skip_reason(price: float, zone: list):
    if price > zone[1]:
        return "above_zone"
    if price < zone[0]:
        return "below_zone"
    return None


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
    entry time. Deterministic policy: shares are the default expression; an
    option is leverage reserved for high-conviction, catalyst-dated theses the
    account can actually trade. Contract viability (a real chain passing every
    filter) is checked separately by the executor — this is only the policy gate.

    Bearish theses have no shares fallback (no shorting): anything short of the
    option gates is 'skip', and the reason is journaled."""
    bearish = t.get("direction") == "bearish"
    fallback = ("skip", ) if bearish else ("shares", )

    def _no(reason):
        return fallback[0], reason

    if not opt_enabled:
        return _no("options_disabled")
    if options_level < 2:
        return _no("options_level")
    if t["conviction"] < min_conviction:
        return _no("conviction_below_bar")
    cd = t.get("catalyst_date")
    if not cd:
        return _no("no_catalyst_date")
    try:
        cat = date.fromisoformat(str(cd))
    except ValueError:
        return _no("bad_catalyst_date")
    if not (today <= cat <= date.fromisoformat(t["expires"])):
        return _no("catalyst_outside_window")
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

def apply_fill(t: dict, fill_price: float, qty: float, order_id, today: date = None) -> dict:
    if t["status"] != "active":
        raise ValueError(f"cannot enter thesis {t['id']} in status {t['status']}")
    today = today or datetime.now(timezone.utc).date()
    t.update(status="entered", entered_at=_now_iso(), entry_price=float(fill_price),
             qty=float(qty), order_id=str(order_id) if order_id else None,
             hwm=float(fill_price), trailing=False,
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
             pnl_dollars=round((fill_price - entry) * (t["qty"] or 0), 4))
    return t


def apply_terminal(t: dict, status: str) -> dict:
    """active -> expired | cancelled."""
    if t["status"] != "active":
        raise ValueError(f"cannot mark {status}: thesis {t['id']} is {t['status']}")
    if status not in ("expired", "cancelled"):
        raise ValueError(f"bad terminal status {status}")
    t.update(status=status, closed_at=_now_iso())
    return t


def apply_revision(t: dict, changes: dict, reasoning: str, today: date) -> dict:
    """Research may revise invalidation_price and/or ttl_days on an ENTERED
    thesis. Records the old values in `revisions`."""
    if t["status"] != "entered":
        raise ValueError(f"cannot revise thesis {t['id']} in status {t['status']}")
    rec = {"ts": _now_iso(), "reasoning": reasoning, "changes": {}}
    if "invalidation_price" in changes and changes["invalidation_price"]:
        rec["changes"]["invalidation_price"] = [t["invalidation_price"], float(changes["invalidation_price"])]
        t["invalidation_price"] = float(changes["invalidation_price"])
    if "ttl_days" in changes and changes["ttl_days"]:
        new_ttl = int(changes["ttl_days"])
        if TTL_RANGE[0] <= new_ttl <= TTL_RANGE[1]:
            rec["changes"]["ttl_days"] = [t["ttl_days"], new_ttl]
            t["ttl_days"] = new_ttl
            t["expires"] = (today + timedelta(days=new_ttl)).isoformat()
    if rec["changes"]:
        t["revisions"].append(rec)
    return t


# --------------------------------------------------------------------------
# Journal event builders (shapes asserted in tests)
# --------------------------------------------------------------------------

def event(kind: str, **payload) -> dict:
    return {"ts": _now_iso(), "event": kind, **payload}
