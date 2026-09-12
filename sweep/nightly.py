#!/usr/bin/env python3
"""Nightly out-of-sample validation. Read-only: never touches live trading.

Refetches bars, re-runs the walk-forward across the farm, appends the result to a
history file, and alerts via ntfy when the out-of-sample number moves materially or
the run fails. Quiet otherwise - a daily "still losing 1.4%" push is noise.

The point is drift detection, not optimisation. A strategy that was validated once
is not validated forever: the market regime changes, and this is the cheap way to
find out. It deliberately does NOT feed anything back into the live config.

  ./nightly.py --days 90 --cost-bps 5
  ./nightly.py --dry-run          # no fetch, no notify - just show what it would do
"""
import argparse, json, os, re, subprocess, sys, time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
VENV = os.path.join(PROJ, "venv", "bin", "python")      # pandas + alpaca live here
HISTORY = os.path.join(HERE, "history", "nightly.jsonl")


def notify(msg, priority="default", dry=False):
    """ntfy.sh, the same channel auto-deploy-main.sh uses."""
    topic = None
    env = os.path.join(PROJ, ".env")
    if os.path.exists(env):
        for line in open(env):
            m = re.match(r"\s*(?:NOTIFY_)?NTFY_TOPIC\s*=\s*(.+)", line)
            if m:
                topic = m.group(1).strip().strip('"').strip("'")
    if dry or not topic:
        print(f"[notify:{priority}] {msg}" + ("" if topic else "  (no NTFY topic configured)"))
        return
    try:
        subprocess.run(["curl", "-s", "-H", f"Priority: {priority}", "-d", msg,
                        f"https://ntfy.sh/{topic}"], capture_output=True, timeout=20)
    except Exception as e:
        print(f"notify failed: {e}", file=sys.stderr)


def load_history():
    if not os.path.exists(HISTORY):
        return []
    return [json.loads(l) for l in open(HISTORY) if l.strip()]


def append_history(rec):
    os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
    with open(HISTORY, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--cost-bps", type=float, default=5.0)
    p.add_argument("--drift-pp", type=float, default=2.0,
                   help="alert if mean out-of-sample return moves this many points "
                        "from the trailing median of previous runs")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    started = datetime.now(timezone.utc).isoformat()
    cache = os.path.join(HERE, "nightly_cache.npz")
    t0 = time.time()

    # 1. fresh data (the whole point - yesterday's cache proves nothing today)
    if not a.dry_run:
        r = run([VENV, os.path.join(HERE, "cache.py"), "--days", str(a.days), "--out", cache],
                cwd=PROJ, timeout=900)
        if r.returncode != 0:
            tail = (r.stderr or r.stdout)[-400:]
            notify(f"[WF] nightly FAILED at fetch: {tail}", "high", a.dry_run)
            append_history({"ts": started, "status": "fetch_failed", "error": tail})
            sys.exit(1)
    elif not os.path.exists(cache):
        cache = os.path.join(HERE, "cache90.npz")       # reuse for a dry run

    # 2. walk-forward; wf.py asks the farm whether to distribute
    wf_cmd = [sys.executable, os.path.join(HERE, "wf.py"), "--walk", "--cache", cache,
              "--cost-bps", str(a.cost_bps)]
    if a.dry_run:
        wf_cmd.append("--no-sync")      # sync is on by default; there is no --sync flag
    r = run(wf_cmd, cwd=HERE, timeout=3600)
    out = r.stdout + r.stderr
    if r.returncode != 0:
        notify(f"[WF] nightly FAILED in walk-forward: {out[-400:]}", "high", a.dry_run)
        append_history({"ts": started, "status": "wf_failed", "error": out[-400:]})
        sys.exit(1)

    m = re.search(r"mean TRAIN ([+-][\d.]+)% \| mean TEST ([+-][\d.]+)% \| "
                  r"median TEST ([+-][\d.]+)% \| TEST>0 (\d+)/(\d+)", out)
    if not m:
        notify(f"[WF] nightly could not parse result", "high", a.dry_run)
        append_history({"ts": started, "status": "parse_failed", "stdout": out[-600:]})
        sys.exit(1)

    train, test, med, pos, folds = (float(m.group(1)), float(m.group(2)), float(m.group(3)),
                                    int(m.group(4)), int(m.group(5)))
    where = "fleet" if "-> FLEET" in out else "local"
    rec = {"ts": started, "status": "ok", "days": a.days, "cost_bps": a.cost_bps,
           "mean_train": train, "mean_test": test, "median_test": med,
           "folds_positive": pos, "folds": folds, "ran_on": where,
           "seconds": round(time.time() - t0, 1)}

    # 3. drift against the trailing median of previous OK runs
    prev = [h["mean_test"] for h in load_history() if h.get("status") == "ok"]
    alert, priority = None, "default"
    if not prev:
        alert = (f"[WF] baseline set: out-of-sample {test:+.2f}% over {folds} folds "
                 f"({pos} positive), {a.cost_bps}bps, ran {where}")
    else:
        prev_sorted = sorted(prev)
        base = prev_sorted[len(prev_sorted) // 2]
        rec["baseline"] = base
        rec["drift_pp"] = round(test - base, 3)
        if abs(test - base) >= a.drift_pp:
            direction = "improved" if test > base else "degraded"
            alert, priority = (f"[WF] out-of-sample {direction}: {test:+.2f}% vs baseline "
                               f"{base:+.2f}% ({test - base:+.2f}pp) over {folds} folds, "
                               f"{a.cost_bps}bps"), "high"

    append_history(rec)
    print(json.dumps(rec, indent=2))
    if alert:
        notify(alert, priority, a.dry_run)
    else:
        print(f"(quiet: {test:+.2f}% is within {a.drift_pp}pp of baseline "
              f"{rec.get('baseline', 0):+.2f}% - no alert)")


if __name__ == "__main__":
    main()
