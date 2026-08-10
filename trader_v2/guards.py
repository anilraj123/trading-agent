"""Account-level trading guards — pure logic, no I/O.

PDT (pattern day trader): only relevant for MARGIN accounts under $25k equity.
The live account is currently CASH (multiplier=1), where PDT does not apply —
settled funds are the binding constraint and Alpaca's reported buying power
already enforces that. This guard therefore ships dormant: the executor wires
it only when account.multiplier > 1, so a future margin conversion activates
it without a deploy.

A day trade = opening and closing the same position in one session. v2's
multi-day thesis holds are naturally safe; the risk is same-day exits on
positions entered today. Policy:
  - exits on positions NOT entered today: never blocked
  - non-critical exits (trailing_stop, take_profit, research_close,
    ttl_expiry, invalidation): deferred to the next session once
    daytrade_count >= soft_max (default 2) — they lose nothing by waiting
  - critical exits (disaster_stop, premium_stop, expiry_force_exit): allowed
    through at count <= 2 (spending the 3rd day trade to cap a loss is
    acceptable) but hard-blocked at 3 — never take the 4th and trip the PDT
    flag (90-day freeze). A blocked critical exit must page the user.
"""

CRITICAL_EXITS = ("disaster_stop", "premium_stop", "expiry_force_exit")


def pdt_exit_blocked(entered_date: str, today_iso: str, daytrade_count: int,
                     reason: str, soft_max: int = 2) -> bool:
    """True if this exit should be deferred to the next session.

    entered_date/today_iso: ISO dates (entered_at[:10] vs session date).
    daytrade_count: broker's rolling 5-day count at cycle start.
    """
    if entered_date != today_iso:
        return False   # multi-day hold: exiting today is not a day trade
    if daytrade_count is None:
        return False   # cash account (Alpaca reports null) — PDT n/a
    if reason in CRITICAL_EXITS:
        return daytrade_count >= 3   # cap a loss up to the 3rd, never the 4th
    return daytrade_count >= soft_max


def pdt_entry_blocked(daytrade_count: int) -> bool:
    """Block opening a position we might be unable to stop out same-day."""
    if daytrade_count is None:
        return False
    return daytrade_count >= 3
