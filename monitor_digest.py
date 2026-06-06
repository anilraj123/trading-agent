#!/usr/bin/env python3
"""Read-only monitoring digest for the trading bots.

Parses the structured logs written to DATA_DIR (cycle_log.jsonl, llm_calls.jsonl)
plus the existing CSVs (trade_log.csv, daily_history.csv) into a human-readable
report: equity, trades/win-rate, rejection reasons, the options funnel, and LLM
token spend. Stdlib only — runnable anywhere after `pull-data.sh`.

Usage:
    python monitor_digest.py                 # all data in ./data (or $DATA_DIR)
    python monitor_digest.py --since 2026-06-01
    python monitor_digest.py --data-dir ./data --days 7
"""
import os
import csv
import json
import argparse
from collections import Counter, defaultdict
from datetime import datetime, date, timedelta


def _data_dir(cli):
    return cli or os.getenv("DATA_DIR") or os.path.join(os.path.dirname(__file__), "data")


def _read_jsonl(path):
    out = []
    if not os.path.isfile(path):
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _read_csv(path):
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def _ts_date(rec, key="ts"):
    try:
        return datetime.fromisoformat(rec[key]).date()
    except Exception:
        return None


def _within(d, since):
    return d is not None and (since is None or d >= since)


def _fmt_num(x, pct=False, plus=False):
    if x is None:
        return "n/a"
    try:
        s = f"{x:+.2f}" if plus else f"{x:.2f}"
        return s + ("%" if pct else "")
    except Exception:
        return str(x)


def section(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def digest(data_dir, since):
    print(f"MONITORING DIGEST — data: {data_dir}" + (f" | since {since}" if since else " | all data"))

    cycles = [c for c in _read_jsonl(os.path.join(data_dir, "cycle_log.jsonl")) if _within(_ts_date(c), since)]
    llm = [c for c in _read_jsonl(os.path.join(data_dir, "llm_calls.jsonl")) if _within(_ts_date(c), since)]
    trades = _read_csv(os.path.join(data_dir, "trade_log.csv"))
    daily = _read_csv(os.path.join(data_dir, "daily_history.csv"))

    def trade_date(t):
        try:
            return datetime.fromisoformat(t["timestamp"]).date()
        except Exception:
            try:
                return date.fromisoformat(t["date"])
            except Exception:
                return None
    trades = [t for t in trades if _within(trade_date(t), since)]

    if not any([cycles, llm, trades, daily]):
        print("\n(no log data found yet — run pull-data.sh after the bots have run a cycle)")
        return

    # ---- Portfolio / equity ----------------------------------------------
    section("PORTFOLIO")
    eq_cycles = [c for c in cycles if c.get("bot") == "trading" and c.get("equity") is not None]
    if eq_cycles:
        latest = eq_cycles[-1]
        print(f"Latest equity: ${_fmt_num(latest.get('equity'))} | cash ${_fmt_num(latest.get('cash'))} | "
              f"positions {latest.get('num_positions')} | regime {latest.get('regime_mode')}")
        eqs = [c["equity"] for c in eq_cycles]
        print(f"Equity range (logged cycles): ${min(eqs):.2f} → ${max(eqs):.2f} (last ${eqs[-1]:.2f})")
    for row in daily[-7:]:
        print(f"  {row.get('date')} [{row.get('bot')}] start ${row.get('start_value')} → end ${row.get('end_value')} "
              f"| pnl ${row.get('pnl')} | trades {row.get('trades')} (W{row.get('wins')}/L{row.get('losses')})")

    # ---- Trades / win rate -----------------------------------------------
    section("TRADES")
    for bot in ("trading", "options"):
        bt = [t for t in trades if t.get("bot") == bot]
        closed = [t for t in bt if t.get("pnl_pct") not in (None, "")]
        wins = sum(1 for t in closed if float(t["pnl_pct"]) >= 0)
        wr = (wins / len(closed) * 100) if closed else 0
        realized = sum(float(t["pnl_dollars"]) for t in bt if t.get("pnl_dollars") not in (None, ""))
        print(f"[{bot:8}] {len(bt)} trades | {len(closed)} closed ({wins}W/{len(closed)-wins}L, {wr:.0f}% win) | realized ${realized:+.2f}")
    recent = sorted(trades, key=lambda t: t.get("timestamp", ""), reverse=True)[:8]
    if recent:
        print("Recent:")
        for t in recent:
            pnl = f" {float(t['pnl_pct']):+.1f}%" if t.get("pnl_pct") not in (None, "") else ""
            print(f"  {t.get('date')} [{t.get('bot')}] {t.get('action')} {t.get('symbol')}{pnl} — {t.get('strategy') or t.get('stop_type') or ''}")

    # ---- Rejection reasons (equity bot) ----------------------------------
    section("REJECTIONS (what the bot wanted but couldn't do)")
    rej = Counter()
    intents = Counter()
    for c in cycles:
        for a in c.get("actions", []) or []:
            if a.get("status") == "rejected":
                rej[a.get("reason", "?")] += 1
            intents[a.get("status", "?")] += 1
    if intents:
        print("Action outcomes: " + ", ".join(f"{k}={v}" for k, v in intents.most_common()))
    if rej:
        for reason, n in rej.most_common():
            print(f"  {n:4} × {reason}")
    else:
        print("  (no rejections logged)")

    # ---- Options funnel ---------------------------------------------------
    section("OPTIONS FUNNEL (where cycles exit)")
    stages = Counter(c.get("stage") for c in cycles if c.get("bot") == "options")
    if stages:
        order = ["position_limit", "total_cap", "no_viable", "no_ta", "hold", "trade"]
        for st in order:
            if stages.get(st):
                print(f"  {stages[st]:4} × {st}")
        for st, n in stages.items():
            if st not in order:
                print(f"  {n:4} × {st}")
    else:
        print("  (no options cycles logged)")

    # ---- LLM usage / cost -------------------------------------------------
    section("LLM CALLS")
    by_day = defaultdict(int)
    tok_in = tok_out = tok_cache = 0
    parse_fail = 0
    models = Counter()
    for c in llm:
        d = _ts_date(c)
        if d:
            by_day[d] += 1
        u = c.get("usage") or {}
        tok_in += u.get("input_tokens") or 0
        tok_out += u.get("output_tokens") or 0
        tok_cache += u.get("cache_read_input_tokens") or 0
        if not c.get("parse_ok", True):
            parse_fail += 1
        if c.get("model"):
            models[c["model"]] += 1
    print(f"Total calls: {len(llm)} | parse failures: {parse_fail}")
    if models:
        print("Models: " + ", ".join(f"{m}×{n}" for m, n in models.most_common()))
    if by_day:
        avg = len(llm) / len(by_day)
        print(f"Calls/day: avg {avg:.0f} over {len(by_day)} days")
    print(f"Tokens: in {tok_in:,} | out {tok_out:,} | cache-read {tok_cache:,}", end="")
    if tok_in + tok_cache:
        print(f" | cache hit {tok_cache / (tok_in + tok_cache) * 100:.0f}%")
    else:
        print()


def main():
    ap = argparse.ArgumentParser(description="Read-only monitoring digest for the trading bots")
    ap.add_argument("--data-dir", default=None, help="path to data dir (default: $DATA_DIR or ./data)")
    ap.add_argument("--since", default=None, help="only include records on/after YYYY-MM-DD")
    ap.add_argument("--days", type=int, default=None, help="only include the last N days")
    args = ap.parse_args()

    since = None
    if args.since:
        since = date.fromisoformat(args.since)
    elif args.days:
        since = date.today() - timedelta(days=args.days)

    digest(_data_dir(args.data_dir), since)


if __name__ == "__main__":
    main()
