"""Estimate HUMAN-DRIVEN Codex sessions so they enter the productivity metric.

Until 2026-08-18 productivity measured Claude Code only — both the baseline and
the human-attention denominator came from ~/.claude transcripts. Codex sessions
contributed dollars and tokens but zero hours, so 19,129 desktop-Codex calls
over 30 days (99.7% of Codex usage, driven by the human) rendered as no work.

Scope, deliberately narrow:
  * only sessions whose rollout meta says a human drove them (Codex Desktop /
    cli). `codex exec` dispatches are launched by Claude from inside a Claude
    session whose baseline already covers that work — estimating them too would
    double-count. See codex_transcript.is_human_driven.
  * only sessions that have no estimate yet (same predicate the SessionStart
    hook uses).

Usage:
    py -3.14 tracker/backfill-codex-estimates.py --list
    py -3.14 tracker/backfill-codex-estimates.py --since 2026-07-01
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRACKER_DIR = PROJECT_ROOT / "tracker"
ESTIMATOR = TRACKER_DIR / "estimate-task.py"
DEFAULT_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(TRACKER_DIR))
import codex_transcript as ct  # noqa: E402

_spec = importlib.util.spec_from_file_location("nl_summary", TRACKER_DIR / "summary.py")
summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(summary)


def needs_estimation(entry) -> bool:
    """Same predicate as hooks/claude-session-start.py — keep in step."""
    if not isinstance(entry, dict):
        return True
    if entry.get("needs_manual_review"):
        return False
    return not entry.get("estimated_at") and entry.get("ai_baseline_hours") is None


def candidates(sessions_root: Path, tasks: dict):
    rows = []
    for path in sorted(sessions_root.rglob("*.jsonl")):
        meta = ct.read_meta(path)
        if not ct.is_human_driven(meta):
            continue
        session_id = str(meta.get("session_id") or "").strip()
        if not session_id:
            continue
        if not needs_estimation(tasks.get(session_id)):
            continue
        stamps = ct.read_human_timestamps(path)
        if not stamps:
            continue
        rows.append((session_id, path, max(stamps), len(stamps)))
    rows.sort(key=lambda row: row[2], reverse=True)
    return rows


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions-root", type=Path, default=DEFAULT_SESSIONS_ROOT)
    parser.add_argument("--since", help="Only sessions last active on/after YYYY-MM-DD.")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--list", action="store_true", help="Show the backlog, estimate nothing.")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)

    if not args.sessions_root.exists():
        print(f"[codex-est] нет каталога сессий: {args.sessions_root}")
        return 1

    tasks = summary.read_tasks()
    rows = candidates(args.sessions_root, tasks)
    if args.since:
        cutoff = datetime.fromisoformat(args.since).date()
        rows = [r for r in rows if r[2].astimezone().date() >= cutoff]
    if args.limit:
        rows = rows[: args.limit]

    if not rows:
        print("[codex-est] нечего оценивать")
        return 0

    print(f"[codex-est] сессий Codex к оценке: {len(rows)}")
    for session_id, path, last, prompts in rows:
        print(f"  {session_id[:8]}  {last.astimezone():%Y-%m-%d %H:%M}  промптов {prompts:>4}  {path.stat().st_size/1e6:.1f} МБ")
    if args.list:
        return 0

    ok = failed = 0
    for index, (session_id, path, last, _prompts) in enumerate(rows, start=1):
        label = f"[{index}/{len(rows)}] {session_id[:8]}"
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
        entry = summary.read_tasks().get(session_id) or {}
        hours = entry.get("ai_baseline_hours")
        if hours is None:
            failed += 1
            print(f"{label} ✗ оценка не записана ({str(entry.get('brief_description', ''))[:60]})")
        else:
            ok += 1
            print(f"{label} ✓ {hours} ч")

    print(f"[codex-est] готово: успешно {ok}, не вышло {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
