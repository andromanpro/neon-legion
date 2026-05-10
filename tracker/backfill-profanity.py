#!/usr/bin/env python
"""Backfill profanity counts (and find transcripts) for every session in
`claude-events.jsonl` whose `tasks.json` entry is missing one. Skips the
oracle call entirely — profanity counting is local and deterministic, so it
shouldn't be gated on Claude CLI auth working in subprocess.

Usage: py -3.14 tracker/backfill-profanity.py
"""
import glob
import importlib
import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "tracker"))

# Load estimate-task.py (filename has dash, not importable normally)
spec = importlib.util.spec_from_file_location("estimate_task", PROJECT_ROOT / "tracker" / "estimate-task.py")
estimate_task = importlib.util.module_from_spec(spec)
spec.loader.exec_module(estimate_task)

import summary  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def find_transcript(session_id: str) -> Path | None:
    """Find <session_id>.jsonl anywhere under ~/.claude/projects/*/."""
    matches = list(CLAUDE_PROJECTS.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def main() -> int:
    events = summary.read_all_events() if hasattr(summary, "read_all_events") else None
    if events is None:
        # Fallback — read JSONL directly
        events_file = PROJECT_ROOT / "tracker" / "claude-events.jsonl"
        events = []
        with events_file.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue

    session_ids = sorted({e.get("session_id") for e in events
                          if isinstance(e.get("session_id"), str) and e.get("session_id")})
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
    for sid in session_ids:
        prev = existing.get(sid) or {}
        if isinstance(prev.get("profanity_count"), int):
            skipped_already += 1
            continue
        transcript = find_transcript(sid)
        if transcript is None:
            skipped_no_transcript += 1
            continue
        try:
            user_messages, _ = estimate_task.read_transcript(transcript)
            profanity = estimate_task.count_profanity(user_messages)
        except Exception as exc:
            print(f"  {sid[:8]}: read failed — {exc}", file=sys.stderr)
            continue
        estimate_task.update_task_entry(sid, {
            "transcript_path": str(transcript),
            "profanity_count": profanity,
        })
        counted += 1
        if profanity > 0:
            print(f"  {sid[:8]}: profanity={profanity}")

    print()
    print(f"Counted: {counted}")
    print(f"Skipped (already had count): {skipped_already}")
    print(f"Skipped (no transcript found): {skipped_no_transcript}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
