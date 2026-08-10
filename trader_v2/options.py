"""Pure options logic for trader_v2 — no I/O, no broker, fully unit-testable.

Design rules (from the overhaul plan, informed by options_bot's post-mortem):
- The thesis drives the strike, never the budget: selection targets ~0.50 delta
  at 30-45 DTE and rejects what doesn't fit, instead of buying whatever premium
  the budget allows (the old bot's far-OTM lottery-ticket failure).
- Spread filter is a % of mid, not absolute dollars — $0.50 is nothing on a
  $20 premium and everything on a $0.60 one.
- Premium stops are DTE-tiered (theta burns faster near expiry), lifted from
  options_bot's one proven idea.
- Exits are decided here; order routing/escalation lives in the executor.
"""
from datetime import date, datetime

# Appended to thesis.EXIT_REASONS when the executor integration lands (PR3).
OPTION_EXIT_REASONS = ("premium_stop", "take_profit", "expiry_force_exit")


# --- OSI symbol handling ----------------------------------------------------

def parse_osi(symbol: str) -> dict | None:
    """Parse an OSI option symbol (e.g. AAPL260918C00190000) into its parts.

    Returns {"underlying", "expiry": date, "type": "call"|"put", "strike": float}
    or None if the symbol isn't a well-formed OSI contract.
    """
    if not symbol or len(symbol) < 16:  # >= 1-char root + 6 date + 1 type + 8 strike
        return None
    root, datepart, cp, strikepart = symbol[:-15], symbol[-15:-9], symbol[-9], symbol[-8:]
    if not root.isalpha() or cp not in ("C", "P") or not strikepart.isdigit():
        return None
    try:
        expiry = datetime.strptime(datepart, "%y%m%d").date()
    except ValueError:
        return None
    return {
        "underlying": root,
        "expiry": expiry,
        "type": "call" if cp == "C" else "put",
        "strike": int(strikepart) / 1000.0,
    }


def dte(expiry: date, today: date) -> int:
    return (expiry - today).days


# --- pricing helpers --------------------------------------------------------

def spread_pct(bid: float, ask: float) -> float | None:
    """Bid/ask spread as a fraction of mid. None when the quote is unusable
    (missing side, zero bid, crossed market)."""
    if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    return (ask - bid) / mid


def mid_price(bid: float, ask: float) -> float | None:
    if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (bid + ask) / 2


def tick_round(price: float, up: bool) -> float:
    """Round a price to a valid option tick: $0.01 below $3.00, $0.05 at/above.
    up=True rounds toward the ask, up=False toward the bid."""
    tick = 0.01 if price < 3.0 else 0.05
    ticks = price / tick
    n = -(-ticks // 1) if up else ticks // 1  # ceil / floor without importing math
    return round(n * tick, 2)


# --- premium stops ----------------------------------------------------------

def parse_stop_tiers(spec: str) -> tuple:
    """'5:-0.25,14:-0.40,-0.55' -> ((5, -0.25), (14, -0.40), (None, -0.55)).
    The last entry may omit the DTE bound and becomes the catch-all."""
    tiers = []
    for part in spec.split(","):
        part = part.strip()
        if ":" in part:
            bound, pct = part.split(":")
            tiers.append((int(bound), float(pct)))
        else:
            tiers.append((None, float(part)))
    if not tiers or tiers[-1][0] is not None:
        raise ValueError(f"stop tiers spec needs a catch-all last entry: {spec!r}")
    bounds = [b for b, _ in tiers[:-1]]
    if bounds != sorted(bounds) or len(set(bounds)) != len(bounds):
        raise ValueError(f"stop tier DTE bounds must be strictly increasing: {spec!r}")
    return tuple(tiers)


def premium_stop_pct(dte_val: int, tiers) -> float:
    """DTE-tiered premium stop. tiers is parse_stop_tiers output."""
    for bound, pct in tiers:
        if bound is None or dte_val <= bound:
            return pct
    return tiers[-1][1]  # unreachable given parse validation; belt-and-braces


# --- contract selection -----------------------------------------------------

def select_contract(rows: list, side: str, *, delta_min: float, delta_max: float,
                    dte_min: int, dte_max: int, min_oi: int, max_spread_pct: float,
                    min_premium: float, max_premium: float) -> tuple:
    """Pick the best contract from normalized chain rows, or explain why not.

    rows: [{"symbol", "type", "strike", "expiry": date, "dte": int, "bid", "ask",
            "mid", "delta", "iv", "oi"}] — built by options_data.fetch_chain_rows.
    side: "call" | "put".

    Filters, then ranks by |delta - 0.50| asc, spread_pct asc, dte desc.
    Returns (row, None) on success or (None, reject_summary) where the summary
    counts why rows were rejected — journaled so silent no-trades are debuggable.
    """
    rejects = {}

    def _reject(reason):
        rejects[reason] = rejects.get(reason, 0) + 1

    candidates = []
    for r in rows:
        if r.get("type") != side:
            continue
        if r.get("delta") is None:
            _reject("no_greeks")
            continue
        # Puts carry negative delta; filter on magnitude.
        d = abs(r["delta"])
        if not (delta_min <= d <= delta_max):
            _reject("delta_range")
            continue
        if not (dte_min <= r["dte"] <= dte_max):
            _reject("dte_range")
            continue
        if (r.get("oi") or 0) < min_oi:
            _reject("low_oi")
            continue
        sp = spread_pct(r.get("bid"), r.get("ask"))
        if sp is None:
            _reject("bad_quote")
            continue
        if sp > max_spread_pct:
            _reject("wide_spread")
            continue
        cost = r["mid"] * 100
        if cost > max_premium:
            _reject("over_budget")
            continue
        if cost < min_premium:
            _reject("under_min_premium")
            continue
        candidates.append((abs(d - 0.50), sp, -r["dte"], r))

    if not candidates:
        summary = ",".join(f"{k}:{v}" for k, v in sorted(rejects.items())) or "empty_chain"
        return None, summary
    candidates.sort(key=lambda c: c[:3])
    return candidates[0][3], None


def contracts_for_budget(premium_mid: float, budget: float, cash: float) -> int:
    """Integer contracts affordable at premium_mid within budget and cash.
    Options are 100-multiplier instruments; no fractional contracts exist."""
    if premium_mid is None or premium_mid <= 0:
        return 0
    cost = premium_mid * 100
    return int(min(budget, cash) // cost)


# --- exit decision ----------------------------------------------------------

def option_exit_decision(t: dict, premium_mid: float, underlying_price: float,
                         dte_val: int, close_window: bool, today: date, *,
                         force_exit_dte: int, stop_tiers, take_profit_pct: float) -> str | None:
    """Exit reason for an entered option thesis, or None to hold.

    Precedence mirrors v2's philosophy: hard risk rails every cycle, judgment
    calls (invalidation on the underlying) only in the close window.

    t: thesis dict with instrument=="option"; entry_price is the PREMIUM paid,
    t["option"]["type"] is "call"/"put", t["expires"] the thesis TTL ISO date.
    """
    if t.get("status") != "entered":
        return None
    opt_type = t["option"]["type"]

    # 1. Expiry force-exit: DTE <= 1 any cycle; DTE <= force_exit_dte at close.
    #    (Clock-derived close_window — never local wall time; the old bot's
    #    naive datetime.now() fired 4 hours early in a UTC container.)
    if dte_val is None or dte_val <= 1 or (close_window and dte_val <= force_exit_dte):
        return "expiry_force_exit"

    entry = t["entry_price"]
    pnl_pct = (premium_mid - entry) / entry if (premium_mid and entry) else None

    # 2. DTE-tiered premium stop, every cycle.
    if pnl_pct is not None and pnl_pct <= premium_stop_pct(dte_val, stop_tiers):
        return "premium_stop"

    # 3. Research said close.
    if t.get("pending_close"):
        return "research_close"

    # 4. Thesis invalidation on the UNDERLYING, close window only (v2's core
    #    lesson: intraday stops on a daily-horizon signal went 0-for-11 in v1).
    if close_window and underlying_price is not None:
        inv = t["invalidation_price"]
        if (opt_type == "call" and underlying_price <= inv) or \
           (opt_type == "put" and underlying_price >= inv):
            return "invalidation"

    # 5. Hard take-profit on premium, any cycle. No premium trailing: hwm
    #    trailing whipsaws on wide-spread instruments.
    if pnl_pct is not None and pnl_pct >= take_profit_pct:
        return "take_profit"

    # 6. Thesis TTL expiry, close window. An option is leverage on a thesis,
    #    never a hold-to-expiry instrument.
    if close_window and today >= date.fromisoformat(t["expires"]):
        return "ttl_expiry"

    return None
