"""Persistence for trader_v2. All state lives under DATA_DIR (the v2 container
sets DATA_DIR=/app/data/v2, fully isolated from v1's files):

  active_theses.json  — non-terminal theses (atomic write)
  journal.jsonl       — append-only event log, the system of record
  lessons.md          — curated lessons memory fed into every research prompt
  run_state.json      — last nightly/weekly/post-mortem markers, halt latch
  baseline.json       — first-run date + paper equity (P&L + SPY benchmark anchor)
"""
import json
import os
from datetime import datetime, timezone

from .config import V2Config

THESES_FILE = f"{V2Config.DATA_DIR}/active_theses.json"
JOURNAL_FILE = f"{V2Config.DATA_DIR}/journal.jsonl"
LESSONS_FILE = f"{V2Config.DATA_DIR}/lessons.md"
RUN_STATE_FILE = f"{V2Config.DATA_DIR}/run_state.json"
BASELINE_FILE = f"{V2Config.DATA_DIR}/baseline.json"


def _ensure_dir():
    os.makedirs(V2Config.DATA_DIR, exist_ok=True)


def _atomic_write_json(path: str, obj: dict):
    _ensure_dir()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


# --- theses ----------------------------------------------------------------

def _migrate_thesis(t: dict) -> dict:
    """Stamp schema-v2 fields onto a legacy (v1-schema) thesis. Idempotent.
    Legacy theses are all long equity, so an entered one is 'shares'."""
    t.setdefault("direction", "bullish")
    t.setdefault("catalyst_date", None)
    t.setdefault("requested_instrument",
                 "option" if t.get("direction") == "bearish" else "shares")
    t.setdefault("instrument", "shares" if t.get("status") == "entered" else None)
    t.setdefault("option", None)
    t.setdefault("multiplier", 1)
    return t


def load_theses() -> list:
    try:
        with open(THESES_FILE) as f:
            return [_migrate_thesis(t) for t in json.load(f).get("theses", [])]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_theses(theses: list):
    live = [t for t in theses if t["status"] in ("active", "entered")]
    _atomic_write_json(THESES_FILE, {
        "version": 2,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "theses": live,
    })


# --- journal ---------------------------------------------------------------

def journal(evt: dict):
    _ensure_dir()
    with open(JOURNAL_FILE, "a") as f:
        f.write(json.dumps(evt) + "\n")


def read_journal_since(ts_iso: str, events: list = None) -> list:
    out = []
    try:
        with open(JOURNAL_FILE) as f:
            for line in f:
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ts_iso and evt.get("ts", "") <= ts_iso:
                    continue
                if events and evt.get("event") not in events:
                    continue
                out.append(evt)
    except FileNotFoundError:
        pass
    return out


# --- lessons memory --------------------------------------------------------

def read_lessons() -> str:
    """Lessons text bounded to LESSONS_MAX_CHARS — truncates OLDEST lines
    first (file is append-ordered), never splitting a line. Hard safety
    independent of the weekly curation."""
    try:
        with open(LESSONS_FILE) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return ""
    kept, size = [], 0
    for line in reversed(lines):        # newest first
        if size + len(line) + 1 > V2Config.LESSONS_MAX_CHARS:
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join(reversed(kept))


def append_lesson(lesson: str, evidence: str, today_iso: str):
    _ensure_dir()
    with open(LESSONS_FILE, "a") as f:
        f.write(f"- [{today_iso}] {lesson} (evidence: {evidence})\n")


def replace_lessons(text: str):
    _ensure_dir()
    tmp = LESSONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(text.rstrip() + "\n")
    os.replace(tmp, LESSONS_FILE)


# --- run state & baseline --------------------------------------------------

def load_run_state() -> dict:
    try:
        with open(RUN_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_nightly_date": None, "last_weekly_date": None,
                "last_postmortem_ts": None, "halted_date": None}


def save_run_state(state: dict):
    _atomic_write_json(RUN_STATE_FILE, state)


def load_baseline() -> dict:
    try:
        with open(BASELINE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def init_baseline(today_iso: str, equity: float) -> dict:
    baseline = {"date": today_iso, "equity": equity}
    _atomic_write_json(BASELINE_FILE, baseline)
    return baseline
