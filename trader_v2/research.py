"""Nightly research: the LLM-as-analyst half of trader_v2.

Sequence (after market close, once per trading day):
  post-mortem (closed trades vs their theses -> lessons)
  -> input assembly (candidates + news + book + lessons)
  -> Sonnet structured call -> validation -> replace model -> persist.

The LLM here never submits an order. Its entire output is a plan the
deterministic executor trades tomorrow. Whole-run failure keeps yesterday's
theses — TTLs decay the stale book safely.
"""
import json
import logging
from datetime import datetime, timezone

from trader.config import Config
from trader.technical_analysis import TechnicalAnalysis
from trader.tracker import log_llm_call
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from . import store
from . import thesis as th
from .config import V2Config

logger = logging.getLogger("trader_v2.research")

RESEARCH_TOOL = {
    "name": "research_output",
    "description": "Tonight's complete thesis set and decisions for held positions",
    "input_schema": {
        "type": "object",
        "properties": {
            "position_decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "thesis_id": {"type": "string"},
                        "action": {"type": "string", "enum": ["keep", "close", "revise"]},
                        "revised_invalidation_price": {"type": "number"},
                        "revised_ttl_days": {"type": "integer"},
                        "reasoning": {"type": "string"},
                    },
                    "required": ["thesis_id", "action", "reasoning"],
                },
            },
            "new_theses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "direction": {"type": "string", "enum": ["bullish", "bearish"]},
                        "conviction": {"type": "integer", "minimum": 1, "maximum": 5},
                        "entry_zone_low": {"type": "number"},
                        "entry_zone_high": {"type": "number"},
                        "invalidation_price": {"type": "number"},
                        "price_target": {"type": "number"},
                        "catalyst": {"type": "string"},
                        "catalyst_date": {"type": "string",
                                          "description": "YYYY-MM-DD of the dated catalyst (earnings, FDA date, event), or empty string if the catalyst has no specific date"},
                        "instrument": {"type": "string", "enum": ["shares", "option"],
                                       "description": "How to express the thesis: 'shares' (default) or 'option' (a ~30-45 DTE near-the-money long call/put). Request 'option' only when you expect a decisive move within the TTL. Bearish theses are always options (long puts)."},
                        "ttl_days": {"type": "integer", "minimum": 1, "maximum": 10},
                        "reasoning": {"type": "string"},
                    },
                    "required": ["symbol", "direction", "conviction", "entry_zone_low",
                                 "entry_zone_high", "invalidation_price", "price_target",
                                 "catalyst", "ttl_days", "reasoning"],
                },
            },
            "market_notes": {"type": "string"},
        },
        "required": ["position_decisions", "new_theses", "market_notes"],
    },
}

POSTMORTEM_TOOL = {
    "name": "postmortem",
    "description": "Durable lessons from reviewing closed trades against their theses",
    "input_schema": {
        "type": "object",
        "properties": {
            "lessons": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "lesson": {"type": "string"},
                        "evidence": {"type": "string"},
                        "n_trades": {"type": "integer", "minimum": 1,
                                     "description": "How many DISTINCT trades (this batch + follow-ups + existing evidence) actually support the lesson. Be honest: 1 means a single observation."},
                    },
                    "required": ["lesson", "evidence", "n_trades"],
                },
            },
        },
        "required": ["lessons"],
    },
}


def _system_prompt(lessons: str) -> str:
    return f"""You are the senior analyst for a small thesis-driven book of stocks and options. A
deterministic executor trades your plan mechanically — you will never be consulted
intraday, so every thesis must stand on its own until tomorrow night.

INSTRUMENT POLICY (you choose the expression; deterministic gates still apply):
- Each thesis carries "instrument": "shares" (the default) or "option". An option
  is one ~30-45 DTE near-the-money contract — a call for bullish, a put for bearish.
  Deterministic code picks the actual contract (delta/OI/spread filters) and will
  fall back to shares (bullish) or skip (bearish) if your option request fails a
  gate: conviction >= {V2Config.OPT_MIN_CONVICTION}, account options level, premium sleeve room, viable contract.
- Request "option" ONLY when you expect a DECISIVE move within the TTL — theta makes
  a slow-drift thesis lose money even when directionally right. A dated catalyst
  (earnings, FDA, launch) is the strongest case; a sharp, volume-confirmed momentum
  setup can also qualify. Set catalyst_date ONLY for genuinely dated events — an
  honest empty date beats a fabricated one.
- Bearish theses can ONLY become long puts (no shorting) and are rejected below
  conviction {V2Config.OPT_MIN_CONVICTION} — do not propose low-conviction bearish theses. For a bearish thesis
  the zone is where the DECLINE starts: invalidation_price sits ABOVE the zone (the
  rally that falsifies it), price_target BELOW it.

EXECUTOR RULES (fixed — write theses that work WITH them):
- It buys ONLY when price is inside your entry_zone. It never chases:
  if price runs beyond the zone, the thesis just sits unfilled until you revise it tonight.
- Your invalidation_price is checked ONLY in the last ~25 min of each session (daily-bar
  discipline; intraday noise is ignored). A -{abs(V2Config.DISASTER_STOP_PCT)*100:.0f}% disaster stop from entry runs intraday.
- Winners: once up {V2Config.TRAIL_ACTIVATE_PCT*100:.0f}%, a {abs(V2Config.TRAIL_STOP_PCT)*100:.0f}% trailing stop from the high-water mark takes over
  and your price_target is advisory. Let the trail do the exit; pick targets to express
  conviction, not to cap winners.
- Unfilled theses expire after ttl_days calendar days.
- RISK CAP (enforced in code): from entry_zone_high down to invalidation_price must be at
  most {V2Config.MAX_RISK_PCT*100:.0f}% (bearish: zone low up to invalidation). The exits above cap winners at
  low single digits, so a 7% invalidation is negative expectancy by construction. A thesis
  breaching the cap is REJECTED; a revision that loosens past it is refused. If the level
  that truly falsifies the thesis is further away, the zone is too high — move the zone
  down to support or skip the name.
- HARD GATES ARE ENFORCED IN CODE. A new thesis is REJECTED outright when its candidate
  row shows volume_ratio < {V2Config.GATE_MIN_VOLUME_RATIO:g}, |chg_5d_pct| > {V2Config.GATE_MAX_MOVE_5D_PCT:g}, or rsi_14 > {V2Config.GATE_MAX_RSI:g}
  (bullish; mirrored for bearish). Sizing down to a lower conviction does NOT get a
  gate-failing name through — do not propose it. Never emit a thesis you intend to
  exclude: omitting it is the only way to exclude it (the executor does not read
  your reasoning).

PORTFOLIO RULES:
- At most {V2Config.MAX_THESES} live theses total (including entered positions), at most
  {V2Config.MAX_POSITIONS} entered at once. Fewer, higher-conviction theses beat more. Conviction 5 is rare.
- ZONES MUST BE REACHABLE. The executor never chases, so a zone the price never
  re-enters simply never fills and the thesis dies unfilled. Default to a zone that
  BRACKETS the last close: low end at support (that's where a GOOD fill happens),
  high end at or slightly above the close. Put the whole zone below the close ONLY
  when a pullback to that exact level is itself the thesis; whole zone above it only
  as a breakout trigger. Always within ±15% of the close.
- "Fill near the low end of the zone" means where WITHIN the zone a fill is good —
  it is NOT an instruction to place the zone below the market.
- invalidation_price must sit below the zone — at a level that FALSIFIES the
  thesis, not an arbitrary percentage.
- Every thesis needs a concrete catalyst or reason the move happens within the TTL.
- Tonight's new_theses list REPLACES all unfilled theses. Re-issue (possibly with a
  fresh zone) anything you still believe in; anything you omit is cancelled.
- Give a decision for EVERY entered position: keep, close, or revise
  (invalidation/ttl only).

{"OPTIONS ARE CURRENTLY DISABLED: every thesis trades as shares; bearish theses cannot be expressed and are rejected — do not propose them, and do not request instrument: option." if not V2Config.OPT_ENABLED else ""}

LESSONS FROM YOUR OWN TRADING HISTORY (earned, do not ignore). A lesson's (n=k) tag is
the number of distinct trades behind it: n < {V2Config.LESSON_MIN_TRADES} is a tentative observation, not a
rule — weigh it accordingly. The hard gates and risk cap are already enforced in code.
{lessons or '(none yet)'}"""


# ---------------------------------------------------------------------------
# Post-mortem
# ---------------------------------------------------------------------------

def _price_now(alpaca, symbol):
    if alpaca is None or not symbol:
        return None
    try:
        return alpaca.get_latest_price(symbol)
    except Exception as e:
        logger.warning(f"follow-up price fetch failed for {symbol}: {e}")
        return None


def exit_followup(evt: dict, price_now) -> dict:
    """How one past exit aged: price now vs the exit fill, as the reviewer
    needs it. since_exit_pct is None for options (exit_price is a premium)
    and when no price is available. Pure — tested."""
    t = evt.get("thesis", {}) or {}
    exit_price = t.get("exit_price")
    row = {
        "symbol": t.get("symbol"), "exit_date": str(evt.get("ts", ""))[:10],
        "exit_reason": t.get("exit_reason") or evt.get("event"),
        "entry_price": t.get("entry_price"), "exit_price": exit_price,
        "pnl_pct": t.get("pnl_pct"), "price_now": price_now, "since_exit_pct": None,
    }
    if price_now and exit_price and t.get("instrument") != "option":
        row["since_exit_pct"] = round((float(price_now) / float(exit_price) - 1) * 100, 2)
    return row


def _followups(alpaca, since: str, days: int) -> list:
    """Exits older than `since` but within `days`, with their post-exit path.
    The post-mortem only ever saw an exit on the night it happened — it
    blamed the trailing stop for flat exits that all preceded further
    declines. Now it also sees how the last N days of exits aged."""
    if days <= 0:
        return []
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    older = [e for e in store.read_journal_since(cutoff, events=["exit"])
             if not since or e.get("ts", "") <= since]
    return [exit_followup(e, _price_now(alpaca, (e.get("thesis") or {}).get("symbol")))
            for e in older[-15:]]


def run_postmortem(llm, notif, alpaca=None):
    run_state = store.load_run_state()
    since = run_state.get("last_postmortem_ts") or ""
    closed = store.read_journal_since(since, events=["exit", "thesis_expired"])
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not closed:
        run_state["last_postmortem_ts"] = now_iso
        store.save_run_state(run_state)
        return

    trades = []
    for evt in closed[-10:]:
        t = evt.get("thesis", {})
        trades.append({
            "symbol": t.get("symbol"), "conviction": t.get("conviction"),
            "catalyst": t.get("catalyst"), "reasoning": t.get("reasoning"),
            "screen_at_creation": t.get("screen"),
            "entry_zone": t.get("entry_zone"), "invalidation": t.get("invalidation_price"),
            "entry_price": t.get("entry_price"), "exit_price": t.get("exit_price"),
            "exit_reason": t.get("exit_reason") or evt.get("event"),
            "pnl_pct": t.get("pnl_pct"),
        })
    followups = _followups(alpaca, since, V2Config.POSTMORTEM_FOLLOWUP_DAYS)
    system = ("You review your own closed trades against their original theses and extract "
              "AT MOST 3 durable, generalizable lessons. An empty list is a fine answer. "
              "Never restate a lesson that already exists. Lessons must be specific enough "
              "to change future behavior ('X pattern fails when Y'), not platitudes. "
              f"Report n_trades honestly — the DISTINCT trades supporting the lesson; "
              f"below {V2Config.LESSON_MIN_TRADES} it is an observation, so never word it as URGENT, HARD, or "
              "a rule. Before judging an EXIT rule (trailing stop, TTL, research close), "
              "check the RECENT EXITS FOLLOW-UP: an exit that preceded a further decline "
              "was right even if it booked a flat or small P&L. The hard gates and risk cap "
              "are enforced in code — do not write lessons that merely restate them.")
    user = (f"EXISTING LESSONS:\n{store.read_lessons() or '(none)'}\n\n"
            f"CLOSED TRADES SINCE LAST REVIEW:\n{json.dumps(trades, indent=1)}\n\n"
            f"RECENT EXITS FOLLOW-UP (how earlier exits aged; since_exit_pct = price now vs exit):\n"
            f"{json.dumps(followups, indent=1) if followups else '(none)'}")
    result = llm.call_structured(POSTMORTEM_TOOL, system=system,
                                 messages=[{"role": "user", "content": user}],
                                 model=V2Config.RESEARCH_MODEL, max_tokens=1000,
                                 temperature=None)
    today_iso = datetime.now(timezone.utc).date().isoformat()
    if isinstance(result, dict) and "lessons" in result:
        for item in result["lessons"][:3]:
            lesson, evidence = str(item.get("lesson", "")), str(item.get("evidence", ""))
            try:
                n_trades = max(1, int(item.get("n_trades") or 1))
            except (TypeError, ValueError):
                n_trades = 1
            if lesson:
                store.append_lesson(lesson, evidence, today_iso, n_trades)
                store.journal(th.event("lesson_added", lesson=lesson, evidence=evidence,
                                       n_trades=n_trades))
    store.journal(th.event("postmortem_run", n_trades=len(trades), n_followups=len(followups),
                           usage=llm.last_usage))
    run_state = store.load_run_state()
    run_state["last_postmortem_ts"] = now_iso
    store.save_run_state(run_state)


# ---------------------------------------------------------------------------
# Input assembly
# ---------------------------------------------------------------------------

def _fetch_news(alpaca, symbols):
    out = {}
    try:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest
        nc = NewsClient(Config.ALPACA_API_KEY, Config.ALPACA_SECRET_KEY)
        for sym in symbols:
            try:
                req = NewsRequest(symbols=sym, limit=V2Config.NEWS_PER_SYMBOL,
                                  exclude_contentless=True)
                articles = nc.get_news(req)
                items = []
                if hasattr(articles, "data"):   # v1's proven access pattern
                    for a in articles.data.get(sym, []):
                        items.append({
                            "headline": getattr(a, "headline", "") or "",
                            "at": str(getattr(a, "created_at", ""))[:10],
                            "summary": (getattr(a, "summary", "") or "")[:200],
                        })
                if items:
                    out[sym] = items
            except Exception as e:
                logger.debug(f"news fetch failed for {sym}: {e}")
    except Exception as e:
        logger.warning(f"news client unavailable: {e}")
    return out


def _assemble_candidates(alpaca, discovery, book_symbols):
    """Top TA-ranked discovery names + everything already in the book.
    Returns (candidates: list[dict], closes: dict[symbol -> last_close])."""
    try:
        universe = discovery.discover_trending_stocks()[:100]
    except Exception as e:
        logger.warning(f"discovery failed: {e}")
        universe = []
    symbols = list(dict.fromkeys(list(book_symbols) + universe))
    bars = alpaca.get_bars_batch(symbols, days=250, timeframe=TimeFrame(1, TimeFrameUnit.Day))
    scored, closes = [], {}
    if bars is None:
        return [], {}
    for sym in symbols:
        try:
            df = bars.xs(sym, level=0) if hasattr(bars.index, "levels") else bars
            if len(df) < 60:
                continue
            ta = TechnicalAnalysis.compute_all(df)
            if not ta:
                continue
            sig = TechnicalAnalysis.score_signals(ta)
            close = float(df["close"].iloc[-1])
            closes[sym] = close
            scored.append({
                "symbol": sym, "close": close,
                "chg_5d_pct": ta.get("momentum_5"),
                "rsi_14": ta.get("rsi_14"),
                "sma_20": ta.get("sma_20"), "sma_50": ta.get("sma_50"),
                "volume_ratio": (ta.get("volume") or {}).get("ratio"),
                "buy_score": sig.get("buy_score"),
                "in_book": sym in book_symbols,
            })
        except Exception:
            continue
    book = [c for c in scored if c["in_book"]]
    rest = sorted([c for c in scored if not c["in_book"]],
                  key=lambda c: -(c["buy_score"] or 0))[:V2Config.RESEARCH_CANDIDATES]
    return book + rest, closes


# ---------------------------------------------------------------------------
# Nightly research
# ---------------------------------------------------------------------------

def kept_count(entered: list) -> int:
    """How many entered positions survive tonight's decisions — journalled as
    `research_run.n_kept`, attribution data the weekly reviewer reads. The
    original expression was `len(entered) - len(decided) + len(decided)`,
    which cancels to `len(entered)` and reported every held position as kept
    even on close-everything nights (found by the AM analyst routine,
    2026-08-24). A position is kept iff research did not flag it for close."""
    return len([t for t in entered if not t.get("pending_close")])


def run_nightly(alpaca, llm, discovery, notif):
    run_postmortem(llm, notif, alpaca)

    theses = store.load_theses()
    entered = [t for t in theses if t["status"] == "entered"]
    actives = [t for t in theses if t["status"] == "active"]
    book_symbols = {t["symbol"] for t in entered + actives}

    candidates, closes = _assemble_candidates(alpaca, discovery, book_symbols)
    if not candidates:
        store.journal(th.event("research_failed", error="no candidates (bars/discovery down)",
                               kept_previous=True))
        notif.send("nightly research: no candidate data — keeping yesterday's theses", priority="high")
        return
    news = _fetch_news(alpaca, [c["symbol"] for c in candidates])

    entered_view = []
    for t in entered:
        price = closes.get(t["symbol"])
        view = {
            "thesis_id": t["id"], "symbol": t["symbol"], "conviction": t["conviction"],
            "direction": t.get("direction", "bullish"),
            "instrument": t.get("instrument", "shares"),
            "entry_price": t["entry_price"], "current_close": price,
            "pnl_pct": round((price / t["entry_price"] - 1) * 100, 2) if price and t["entry_price"] else None,
            "invalidation_price": t["invalidation_price"], "expires": t["expires"],
            "trailing": t["trailing"], "hwm": t["hwm"],
            "catalyst": t["catalyst"], "reasoning": t["reasoning"],
        }
        if t.get("instrument") == "option" and t.get("option"):
            # entry_price/pnl here are PREMIUM values; current_close is the
            # underlying's close — the analyst judges the thesis on the
            # underlying, the executor manages the premium mechanically.
            view["option"] = {k: t["option"].get(k) for k in
                              ("contract", "type", "strike", "expiry", "entry_delta")}
            view["pnl_pct"] = None  # premium P&L isn't derivable from the underlying close
        entered_view.append(view)
    unfilled_view = [{
        "symbol": t["symbol"], "conviction": t["conviction"], "entry_zone": t["entry_zone"],
        "expires": t["expires"], "catalyst": t["catalyst"],
        "instrument": t.get("requested_instrument", "shares"),
        "last_skip": t.get("last_skip"),
    } for t in actives]

    # The analyst reasons about sizing against this number, so it must be the
    # SAME anchor the executor sizes orders with (capital_base honours
    # V2_CAPITAL_MODE) — not the static default. First live night: analyst was
    # told $1350 while the executor sized on $1032 of real equity.
    try:
        capital = V2Config.capital_base(alpaca.get_portfolio_value())
    except Exception as e:
        logger.warning(f"equity fetch for research prompt failed: {e}")
        capital = V2Config.CAPITAL
    user = json.dumps({
        "account": {"capital": round(capital, 2),
                    "max_theses": V2Config.MAX_THESES,
                    "max_positions": V2Config.MAX_POSITIONS,
                    "entered_positions": len(entered)},
        "entered_positions": entered_view,
        "unfilled_theses_yesterday": unfilled_view,
        "candidates": candidates,
        "news": news,
        "instruction": ("Give a decision for every entered position, then output your "
                        "complete desired set of theses for the unfilled slots."),
    }, indent=1, default=str)

    system = _system_prompt(store.read_lessons())
    result = llm.call_structured(RESEARCH_TOOL, system=system,
                                 messages=[{"role": "user", "content": user}],
                                 model=V2Config.RESEARCH_MODEL, max_tokens=3000,
                                 temperature=None)
    log_llm_call("v2_research", llm.last_model or V2Config.RESEARCH_MODEL, system, user,
                 json.dumps(result, default=str), usage=llm.last_usage,
                 parse_ok=isinstance(result, dict) and "error" not in result)

    if not isinstance(result, dict) or "error" in result or "new_theses" not in result:
        store.journal(th.event("research_failed", error=str(result)[:500], kept_previous=True))
        notif.send("nightly research FAILED — keeping yesterday's theses", priority="high")
        return

    today = datetime.now(timezone.utc).date()

    # --- position decisions -------------------------------------------------
    by_id = {t["id"]: t for t in entered}
    for d in result.get("position_decisions", []):
        t = by_id.get(d.get("thesis_id"))
        if t is None:
            store.journal(th.event("error", where="position_decision",
                                   message=f"unknown thesis_id {d.get('thesis_id')}"))
            continue
        action = d.get("action")
        if action == "close":
            t["pending_close"] = True
            store.journal(th.event("thesis_revised", thesis_id=t["id"],
                                   changes={"pending_close": [False, True]},
                                   reasoning=d.get("reasoning", "")))
        elif action == "revise":
            th.apply_revision(t, {"invalidation_price": d.get("revised_invalidation_price"),
                                  "ttl_days": d.get("revised_ttl_days")},
                              d.get("reasoning", ""), today,
                              max_risk_pct=V2Config.MAX_RISK_PCT)
            last = t["revisions"][-1] if t["revisions"] else {}
            store.journal(th.event("thesis_revised", thesis_id=t["id"],
                                   changes=last.get("changes", {}),
                                   rejected=last.get("rejected"),
                                   reasoning=d.get("reasoning", "")))
        # keep -> no-op

    # --- new theses: validate, select, replace unfilled book ---------------
    cand_by_sym = {c["symbol"]: c for c in candidates}
    candidate_syms = set(cand_by_sym)
    entered_syms = {t["symbol"] for t in entered}
    validated, errors = [], []
    for raw in result.get("new_theses", []):
        sym = str(raw.get("symbol", "")).upper()
        err = th.validate_new_thesis(raw, closes.get(sym),
                                     candidate_syms, entered_syms,
                                     blacklist=set(Config.BLACKLIST),
                                     crypto_suffixes=tuple(Config.CRYPTO_SUFFIXES),
                                     today=today,
                                     opt_min_conviction=V2Config.OPT_MIN_CONVICTION,
                                     max_risk_pct=V2Config.MAX_RISK_PCT,
                                     opt_enabled=V2Config.OPT_ENABLED)
        if err:
            errors.append(err)
            continue
        # Hard gates are code, not prompt: the analyst repeatedly sized a
        # gate-failing name down to conviction 3 instead of skipping it.
        gate = th.hard_gate_reason(raw.get("direction", "bullish"), cand_by_sym.get(sym),
                                   min_volume_ratio=V2Config.GATE_MIN_VOLUME_RATIO,
                                   max_move_5d_pct=V2Config.GATE_MAX_MOVE_5D_PCT,
                                   max_rsi=V2Config.GATE_MAX_RSI)
        if gate:
            errors.append(f"{sym}: hard gate — {gate}")
            continue
        validated.append(th.build_thesis(raw, today, screen=th.screen_snapshot(cand_by_sym.get(sym))))
    capacity = max(0, V2Config.MAX_THESES - len(entered))
    selected = th.select_theses(validated, capacity)

    # replace model: cancel unfilled actives not re-issued tonight
    selected_syms = {t["symbol"] for t in selected}
    for t in actives:
        if t["status"] == "active":
            reason = "superseded" if t["symbol"] in selected_syms else "dropped"
            th.apply_terminal(t, "cancelled")
            store.journal(th.event("thesis_cancelled", thesis=t, reason=reason))
    for t in selected:
        # zone_gap_pct: reachability of tonight's zone vs the close the analyst
        # saw — a persistently negative gap means pullback-only zones the
        # no-chase executor may never fill.
        store.journal(th.event("thesis_created", thesis=t,
                               zone_gap_pct=th.zone_gap_pct(
                                   t["entry_zone"], closes.get(t["symbol"]))))

    new_book = [t for t in theses if t["status"] == "entered"] + selected
    store.save_theses(new_book)
    store.journal(th.event("research_run", model=llm.last_model, usage=llm.last_usage,
                           n_new=len(selected), n_kept=kept_count(entered),
                           n_cancelled=len(actives), n_rejected=len(errors),
                           validation_errors=errors,
                           market_notes=str(result.get("market_notes", ""))[:500]))

    lines = [f"{t['symbol']} c{t['conviction']} zone {t['entry_zone'][0]}-{t['entry_zone'][1]}"
             for t in selected]
    notif.send("research done: "
               f"{len(selected)} theses ({'; '.join(lines) or 'none'}), "
               f"{len(errors)} rejected. {str(result.get('market_notes', ''))[:200]}")


# ---------------------------------------------------------------------------
# Weekly deep review (Opus)
# ---------------------------------------------------------------------------

def coverage_note(journal_start: str, cutoff_iso: str, n_events: int) -> str:
    """One line telling the weekly reviewer how much of the 28-day window the
    journal actually covers. A journal that begins INSIDE the window (fresh
    DATA_DIR after a cutover/migration) must not be read as a month of
    inactivity — on the first live weekly review, Opus did exactly that:
    treated an hours-old journal as 'a month with zero trades', minted a false
    lesson from it, and dropped a true one whose evidence predated the file."""
    if not journal_start:
        return ("JOURNAL COVERAGE: the journal is EMPTY — there is NO activity data at all. "
                "Do not draw any conclusion about trading frequency or filter behavior.")
    if journal_start > cutoff_iso:
        return (f"JOURNAL COVERAGE: the journal only begins at {journal_start} — it covers "
                f"PART of the 28-day window (fresh data dir, e.g. after a cutover). Events "
                f"before that are in a prior journal you cannot see. Do not interpret the "
                f"missing period as inactivity, and do not drop lessons whose evidence "
                f"predates {journal_start}. {n_events} events follow.")
    return f"JOURNAL COVERAGE: full 28-day window covered ({n_events} events)."


def run_weekly(llm, notif, alpaca=None):
    # trailing 4 weeks of journal, compacted
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=28)).strftime("%Y-%m-%dT%H:%M:%SZ")
    followups = _followups(alpaca, "", 28)
    events = store.read_journal_since(cutoff, events=["exit", "entry", "circuit_breaker",
                                                      "research_run", "thesis_expired"])
    compact = []
    for e in events[-200:]:
        c = {k: e.get(k) for k in ("ts", "event", "symbol", "reason", "pnl_pct", "pnl_dollars")
             if e.get(k) is not None}
        compact.append(c)
    system = ("You are the weekly strategy reviewer for a thesis-driven equity bot. Review the "
              "trailing month of activity and the current lessons file. Honour the JOURNAL "
              "COVERAGE line: a period the journal does not cover is UNKNOWN, not inactive, "
              "and lessons whose evidence predates the journal must be kept, not dropped. "
              "Lessons carry an (n=k) tag = distinct trades behind them: PRESERVE the tags, "
              "sum distinct trades when you merge duplicates, and never escalate a lesson to "
              f"URGENT/HARD/rule wording below n={V2Config.LESSON_MIN_TRADES}. Judge exit rules "
              "against the EXITS FOLLOW-UP (price now vs exit): an exit followed by a further "
              "decline was right regardless of its booked P&L. Hard gates and the risk cap "
              "are enforced in code; lessons that merely restate them are noise. "
              "Output EXACTLY two "
              "sections:\n=== OBSERVATIONS ===\n(strategy-level observations, what is/isn't "
              "working, 5-10 bullets)\n=== LESSONS ===\n(the COMPLETE rewritten lessons file: "
              "curated, deduplicated, max 20 bullets, keep the '- [date] lesson (evidence: ...)' "
              "format, drop stale or disproven lessons)")
    user = (f"{coverage_note(store.journal_start_ts(), cutoff, len(events))}\n\n"
            f"CURRENT LESSONS FILE:\n{store.read_lessons() or '(none)'}\n\n"
            f"EXITS FOLLOW-UP (how each exit aged):\n{json.dumps(followups, indent=0) if followups else '(none)'}\n\n"
            f"LAST 4 WEEKS OF EVENTS:\n{json.dumps(compact, indent=0)}")
    text = llm.call(system=system, messages=[{"role": "user", "content": user}],
                    model=V2Config.WEEKLY_MODEL, temperature=None, max_tokens=2500)
    rewrote = False
    if "=== LESSONS ===" in text:
        obs, lessons = text.split("=== LESSONS ===", 1)
        obs = obs.replace("=== OBSERVATIONS ===", "").strip()
        if lessons.strip():
            store.replace_lessons(lessons.strip())
            rewrote = True
    else:
        obs = text.strip()
    store.journal(th.event("weekly_review", usage=llm.last_usage, lessons_rewritten=rewrote))
    notif.send(f"WEEKLY REVIEW\n{obs[:900]}", priority="low")
