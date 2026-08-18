"""Estimate sessions that the SessionStart hook skipped while it was disabled.

Until 2026-08-18 the hook queued a session only when it was absent from
tasks.json. backfill-sentiment-markers.py creates the entry on every deploy,
so from 2026-08-01 onward every session was "already present" and never got a
baseline — 91 sessions carry an entry with no hours, and the productivity
windows dropped them from BOTH sides of the ratio (no baseline in the
numerator, no attention in the denominator).

The hook predicate is fixed (hooks/claude-session-start.py::needs_estimation);
this script drains the backlog that accumulated meanwhile. It only touches
sessions whose transcript is still on disk — the rest are unrecoverable.

Usage:
    py -3.14 tracker/backfill-stranded-estimates.py --list
    py -3.14 tracker/backfill-stranded-estimates.py --since 2026-08-01
    py -3.14 tracker/backfill-stranded-estimates.py --since 2026-08-01 --limit 5
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ESTIMATOR = PROJECT_ROOT / "tracker" / "estimate-task.py"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_spec = importlib.util.spec_from_file_location(
    "nl_summary", PROJECT_ROOT / "tracker" / "summary.py"
)
summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(summary)


def stranded_sessions(tasks: dict) -> list[tuple[str, Path, datetime]]:
    """(session_id, transcript, last_activity) for entries with no estimate."""
    out = []
    for session_id, entry in tasks.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("needs_manual_review"):
            continue
        if entry.get("estimated_at") or entry.get("ai_baseline_hours") is not None:
            continue
        transcript = entry.get("transcript_path")
        if not transcript:
            continue
        path = Path(transcript)
        if not path.exists():
            continue
        stamps = summary.read_human_message_timestamps(path)
        if not stamps:
            continue
        out.append((session_id, path, max(stamps)))
    out.sort(key=lambda item: item[2], reverse=True)
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="Only sessions last active on/after this date (YYYY-MM-DD).")
    parser.add_argument("--limit", type=int, help="Estimate at most N sessions.")
    parser.add_argument("--list", action="store_true", help="Show the backlog, estimate nothing.")
    parser.add_argument("--timeout", type=int, default=300, help="Per-session timeout, seconds.")
    args = parser.parse_args(argv)

    tasks = summary.read_tasks()
    backlog = stranded_sessions(tasks)
    if args.since:
        cutoff = datetime.fromisoformat(args.since).date()
        backlog = [row for row in backlog if row[2].astimezone().date() >= cutoff]
    if args.limit:
        backlog = backlog[: args.limit]

    if not backlog:
        print("[stranded] нечего досчитывать")
        return 0

    print(f"[stranded] к досчёту: {len(backlog)} сессий")
    for session_id, path, last in backlog:
        print(f"  {session_id[:8]}  последняя активность {last.astimezone():%Y-%m-%d %H:%M}")
    if args.list:
        return 0

    ok = failed = 0
    for index, (session_id, path, last) in enumerate(backlog, start=1):
        label = f"[{index}/{len(backlog)}] {session_id[:8]}"
        try:
            completed = subprocess.run(
                ["py", "-3.14", str(ESTIMATOR), session_id, str(path)],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=args.timeout,
            )
        except subprocess.TimeoutExpired:
            failed += 1
            print(f"{label} ✗ таймаут {args.timeout}s")
            continue
        if completed.returncode != 0:
            failed += 1
            print(f"{label} ✗ rc={completed.returncode} {completed.stderr.strip()[:160]}")
            continue
        # Re-read to confirm the estimate actually landed (the estimator can
        # exit 0 after writing a manual-review entry).
        entry = summary.read_tasks().get(session_id) or {}
        hours = entry.get("ai_baseline_hours")
        if hours is None:
            failed += 1
            print(f"{label} ✗ оценка не записана ({entry.get('brief_description', '')[:60]})")
        else:
            ok += 1
            print(f"{label} ✓ {hours} ч")

    print(f"[stranded] готово: успешно {ok}, не вышло {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
