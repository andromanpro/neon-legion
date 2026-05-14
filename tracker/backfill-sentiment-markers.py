#!/usr/bin/env python
"""Backfill profanity_count + appreciation_count for every session in
`claude-events.jsonl`. Local & deterministic — no oracle / LLM call required.

Sibling to (and replacement for) `backfill-profanity.py`:
- Counts BOTH profanity and appreciation per session
- Default mode `--force`: re-counts every session, not just ones missing
  the field (use this after extending the lexicon in `estimate-task.py`)
- Default mode `--skip-existing`: behaves like the old backfill-profanity.py

Usage:
  py -3.14 tracker/backfill-sentiment-markers.py            # force-recount all
  py -3.14 tracker/backfill-sentiment-markers.py --skip-existing
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "tracker"))

# Load estimate-task.py (filename has dash, not importable normally)
spec = importlib.util.spec_from_file_location(
    "estimate_task", PROJECT_ROOT / "tracker" / "estimate-task.py"
)
estimate_task = importlib.util.module_from_spec(spec)
spec.loader.exec_module(estimate_task)

import summary  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def find_transcript(session_id: str) -> Path | None:
    matches = list(CLAUDE_PROJECTS.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Only recount sessions missing one of the fields. Default: force re-count.",
    )
    args = parser.parse_args()

    events_file = PROJECT_ROOT / "tracker" / "claude-events.jsonl"
    events: list[dict] = []
    with events_file.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                continue

    session_ids = sorted({
        e.get("session_id") for e in events
        if isinstance(e.get("session_id"), str) and e.get("session_id")
    })
    print(f"Sessions in events: {len(session_ids)}")

    tasks_file = PROJECT_ROOT / "tracker" / "tasks.json"
    if tasks_file.exists():
        with tasks_file.open(encoding="utf-8") as f:
            existing = json.load(f)
    else:
        existing = {}

    counted = 0
    skipped_no_transcript = 0
    skipped_already = 0
    profanity_total = 0
    appreciation_total = 0
    sessions_with_profanity = 0
    sessions_with_appreciation = 0

    for sid in session_ids:
        prev = existing.get(sid) or {}
        if args.skip_existing and isinstance(prev.get("profanity_count"), int) and isinstance(prev.get("appreciation_count"), int):
            skipped_already += 1
            continue

        transcript = find_transcript(sid)
        if transcript is None:
            skipped_no_transcript += 1
            continue

        try:
            user_messages, _ = estimate_task.read_transcript(transcript)
        except Exception as exc:
            print(f"  {sid[:8]}: read failed — {exc}", file=sys.stderr)
            continue

        profanity = estimate_task.count_profanity(user_messages)
        appreciation = estimate_task.count_appreciation(user_messages)

        estimate_task.update_task_entry(sid, {
            "transcript_path": str(transcript),
            "profanity_count": profanity,
            "appreciation_count": appreciation,
        })
        counted += 1
        profanity_total += profanity
        appreciation_total += appreciation
        if profanity > 0:
            sessions_with_profanity += 1
        if appreciation > 0:
            sessions_with_appreciation += 1
        if profanity > 0 or appreciation > 0:
            print(f"  {sid[:8]}: profanity={profanity} appreciation={appreciation}")

    print()
    print(f"Counted: {counted}")
    print(f"Skipped (already had counts): {skipped_already}")
    print(f"Skipped (no transcript found): {skipped_no_transcript}")
    print(f"Totals: profanity={profanity_total} appreciation={appreciation_total}")
    print(
        f"Coverage: {sessions_with_profanity}/{counted} sessions had profanity, "
        f"{sessions_with_appreciation}/{counted} had appreciation"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
