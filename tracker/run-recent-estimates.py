#!/usr/bin/env python
"""Run Phase 1.3 oracle estimation for the N most recent unprocessed sessions.

Why limited: each estimate is a `codex exec` call that burns ChatGPT-quota
(xhigh reasoning, ~10k tokens per session). Backfilling all 250+ sessions in
one shot is impractical. This script picks the most recent sessions whose
`tasks.json` entry has `ai_baseline_hours == None` (or no entry at all) and
runs estimator on each.

Codex review E1: candidates come from the **events list**
(`tracker/claude-events.jsonl`), not from `tasks.json`. Sessions that hooks
missed entirely have no tasks entry yet — they would never be estimated if
we only iterated tasks.

Designed for manual backfill runs. Picks up sessions that the SessionStart hook
missed (e.g. user killed Claude without reopening).

Usage:
  py -3.14 tracker/run-recent-estimates.py [--limit N]

Defaults: --limit 5
"""
import argparse
import importlib.util
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "tracker"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
EVENTS_FILE = PROJECT_ROOT / "tracker" / "claude-events.jsonl"
TASKS_FILE = PROJECT_ROOT / "tracker" / "tasks.json"


def find_transcript(session_id: str) -> Path | None:
    matches = list(CLAUDE_PROJECTS.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def read_session_last_ts() -> dict[str, str]:
    """Scan claude-events.jsonl, return {session_id: latest_ts}."""
    last_ts: dict[str, str] = {}
    if not EVENTS_FILE.exists():
        return last_ts
    with EVENTS_FILE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = ev.get("session_id")
            ts = ev.get("ts")
            if ev.get("model") == "<synthetic>":
                continue
            if not isinstance(sid, str) or not isinstance(ts, str):
                continue
            prev = last_ts.get(sid)
            if prev is None or ts > prev:
                last_ts[sid] = ts
    return last_ts


def read_tasks() -> dict:
    if not TASKS_FILE.exists():
        return {}
    with TASKS_FILE.open(encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5,
                        help="max number of recent sessions to estimate (default 5)")
    args = parser.parse_args()

    # E1: enumerate sessions from events JSONL, not tasks.json — sessions that
    # hooks completely missed have no tasks entry yet and would otherwise be
    # invisible to backfill.
    session_last_ts = read_session_last_ts()
    if not session_last_ts:
        print("No session events found.")
        return 0

    tasks = read_tasks()

    # Candidate = (last_ts, session_id, transcript) where the session
    # has no completed estimate and a discoverable transcript.
    candidates = []
    for sid, last_ts in session_last_ts.items():
        entry = tasks.get(sid) or {}
        if entry.get("ai_baseline_hours") is not None or entry.get("human_corrected_hours") is not None:
            continue
        transcript = find_transcript(sid)
        if transcript is None:
            continue
        candidates.append((last_ts, sid, transcript))

    # E4: sort by last event timestamp (from events file), not file mtime.
    candidates.sort(reverse=True)
    total_candidates = len(candidates)
    candidates = candidates[: args.limit]

    print(f"Pending estimates: {total_candidates} total; running {len(candidates)} (limit={args.limit})")

    successes = failures = 0
    for last_ts, sid, transcript in candidates:
        print(f"  {sid[:8]}: estimating ({transcript.name}, last_ts={last_ts[:19]})...")
        cmd = [sys.executable, str(PROJECT_ROOT / "tracker" / "estimate-task.py"),
               sid, str(transcript)]
        try:
            cp = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace",
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
            if cp.returncode == 0:
                successes += 1
                print("    ok")
            else:
                failures += 1
                print(f"    FAILED rc={cp.returncode}: {cp.stderr.strip()[:200]}")
        except subprocess.TimeoutExpired:
            failures += 1
            print("    TIMEOUT after 300s")

    print()
    print(f"Successes: {successes}")
    print(f"Failures: {failures}")
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
