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
                        "conviction": {"type": "integer", "minimum": 1, "maximum": 5},
                        "entry_zone_low": {"type": "number"},
                        "entry_zone_high": {"type": "number"},
                        "invalidation_price": {"type": "number"},
                        "price_target": {"type": "number"},
                        "catalyst": {"type": "string"},
                        "ttl_days": {"type": "integer", "minimum": 1, "maximum": 10},
                        "reasoning": {"type": "string"},
                    },
                    "required": ["symbol", "conviction", "entry_zone_low", "entry_zone_high",
                                 "invalidation_price", "price_target", "catalyst",
                                 "ttl_days", "reasoning"],
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
                    },
                    "required": ["lesson", "evidence"],
                },
            },
        },
        "required": ["lessons"],
    },
}


def _system_prompt(lessons: str) -> str:
    return f"""You are the senior analyst for a small thesis-driven equity book. A deterministic
executor trades your plan mechanically — you will never be consulted intraday, so every
thesis must stand on its own until tomorrow night.

EXECUTOR RULES (fixed — write theses that work WITH them):
- Long only. It buys ONLY when price is inside your entry_zone. It never chases:
  if price runs above the zone, the thesis just sits unfilled until you revise it tonight.
- Your invalidation_price is checked ONLY in the last ~25 min of each session (daily-bar
  discipline; intraday noise is ignored). A -{abs(V2Config.DISASTER_STOP_PCT)*100:.0f}% disaster stop from entry runs intraday.
- Winners: once up {V2Config.TRAIL_ACTIVATE_PCT*100:.0f}%, a {abs(V2Config.TRAIL_STOP_PCT)*100:.0f}% trailing stop from the high-water mark takes over
  and your price_target is advisory. Let the trail do the exit; pick targets to express
  conviction, not to cap winners.
- Unfilled theses expire after ttl_days calendar days.

PORTFOLIO RULES:
- At most {V2Config.MAX_THESES} live theses total (including entered positions), at most
  {V2Config.MAX_POSITIONS} entered at once. Fewer, higher-conviction theses beat more. Conviction 5 is rare.
- entry_zone may sit below the last close (pullback entry) or above it (breakout
  confirmation), but within ±15% of the close. invalidation_price must sit below the
  zone — at a level that FALSIFIES the thesis, not an arbitrary percentage.
- Every thesis needs a concrete catalyst or reason the move happens within the TTL.
- Tonight's new_theses list REPLACES all unfilled theses. Re-issue (possibly with a
  fresh zone) anything you still believe in; anything you omit is cancelled.
- Give a decision for EVERY entered position: keep, close, or revise
  (invalidation/ttl only).

LESSONS FROM YOUR OWN TRADING HISTORY (earned, do not ignore):
{lessons or '(none yet)'}"""


# ---------------------------------------------------------------------------
# Post-mortem
# ---------------------------------------------------------------------------

def run_postmortem(llm, notif):
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
            "entry_zone": t.get("entry_zone"), "invalidation": t.get("invalidation_price"),
            "entry_price": t.get("entry_price"), "exit_price": t.get("exit_price"),
            "exit_reason": t.get("exit_reason") or evt.get("event"),
            "pnl_pct": t.get("pnl_pct"),
        })
    system = ("You review your own closed trades against their original theses and extract "
              "AT MOST 3 durable, generalizable lessons. An empty list is a fine answer. "
              "Never restate a lesson that already exists. Lessons must be specific enough "
              "to change future behavior ('X pattern fails when Y'), not platitudes.")
    user = (f"EXISTING LESSONS:\n{store.read_lessons() or '(none)'}\n\n"
            f"CLOSED TRADES SINCE LAST REVIEW:\n{json.dumps(trades, indent=1)}")
    result = llm.call_structured(POSTMORTEM_TOOL, system=system,
                                 messages=[{"role": "user", "content": user}],
                                 model=V2Config.RESEARCH_MODEL, max_tokens=1000)
    today_iso = datetime.now(timezone.utc).date().isoformat()
    if isinstance(result, dict) and "lessons" in result:
        for item in result["lessons"][:3]:
            lesson, evidence = str(item.get("lesson", "")), str(item.get("evidence", ""))
            if lesson:
                store.append_lesson(lesson, evidence, today_iso)
                store.journal(th.event("lesson_added", lesson=lesson, evidence=evidence))
    store.journal(th.event("postmortem_run", n_trades=len(trades), usage=llm.last_usage))
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

def run_nightly(alpaca, llm, discovery, notif):
    run_postmortem(llm, notif)

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
        entered_view.append({
            "thesis_id": t["id"], "symbol": t["symbol"], "conviction": t["conviction"],
            "entry_price": t["entry_price"], "current_close": price,
            "pnl_pct": round((price / t["entry_price"] - 1) * 100, 2) if price and t["entry_price"] else None,
            "invalidation_price": t["invalidation_price"], "expires": t["expires"],
            "trailing": t["trailing"], "hwm": t["hwm"],
            "catalyst": t["catalyst"], "reasoning": t["reasoning"],
        })
    unfilled_view = [{
        "symbol": t["symbol"], "conviction": t["conviction"], "entry_zone": t["entry_zone"],
        "expires": t["expires"], "catalyst": t["catalyst"],
        "last_skip": t.get("last_skip"),
    } for t in actives]

    user = json.dumps({
        "account": {"capital": V2Config.CAPITAL,
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
                                 model=V2Config.RESEARCH_MODEL, max_tokens=3000)
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
    decided = set()
    for d in result.get("position_decisions", []):
        t = by_id.get(d.get("thesis_id"))
        if t is None:
            store.journal(th.event("error", where="position_decision",
                                   message=f"unknown thesis_id {d.get('thesis_id')}"))
            continue
        decided.add(t["id"])
        action = d.get("action")
        if action == "close":
            t["pending_close"] = True
            store.journal(th.event("thesis_revised", thesis_id=t["id"],
                                   changes={"pending_close": [False, True]},
                                   reasoning=d.get("reasoning", "")))
        elif action == "revise":
            th.apply_revision(t, {"invalidation_price": d.get("revised_invalidation_price"),
                                  "ttl_days": d.get("revised_ttl_days")},
                              d.get("reasoning", ""), today)
            store.journal(th.event("thesis_revised", thesis_id=t["id"],
                                   changes=t["revisions"][-1]["changes"] if t["revisions"] else {},
                                   reasoning=d.get("reasoning", "")))
        # keep -> no-op

    # --- new theses: validate, select, replace unfilled book ---------------
    candidate_syms = {c["symbol"] for c in candidates}
    entered_syms = {t["symbol"] for t in entered}
    validated, errors = [], []
    for raw in result.get("new_theses", []):
        err = th.validate_new_thesis(raw, closes.get(str(raw.get("symbol", "")).upper()),
                                     candidate_syms, entered_syms,
                                     blacklist=set(Config.BLACKLIST),
                                     crypto_suffixes=tuple(Config.CRYPTO_SUFFIXES))
        if err:
            errors.append(err)
        else:
            validated.append(th.build_thesis(raw, today))
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
        store.journal(th.event("thesis_created", thesis=t))

    new_book = [t for t in theses if t["status"] == "entered"] + selected
    store.save_theses(new_book)
    store.journal(th.event("research_run", model=llm.last_model, usage=llm.last_usage,
                           n_new=len(selected), n_kept=len(entered) - len(decided) + len(decided),
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

def run_weekly(llm, notif):
    # trailing 4 weeks of journal, compacted
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=28)).strftime("%Y-%m-%dT%H:%M:%SZ")
    events = store.read_journal_since(cutoff, events=["exit", "entry", "circuit_breaker",
                                                      "research_run", "thesis_expired"])
    compact = []
    for e in events[-200:]:
        c = {k: e.get(k) for k in ("ts", "event", "symbol", "reason", "pnl_pct", "pnl_dollars")
             if e.get(k) is not None}
        compact.append(c)
    system = ("You are the weekly strategy reviewer for a thesis-driven equity bot. Review the "
              "trailing month of activity and the current lessons file. Output EXACTLY two "
              "sections:\n=== OBSERVATIONS ===\n(strategy-level observations, what is/isn't "
              "working, 5-10 bullets)\n=== LESSONS ===\n(the COMPLETE rewritten lessons file: "
              "curated, deduplicated, max 20 bullets, keep the '- [date] lesson (evidence: ...)' "
              "format, drop stale or disproven lessons)")
    user = (f"CURRENT LESSONS FILE:\n{store.read_lessons() or '(none)'}\n\n"
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
