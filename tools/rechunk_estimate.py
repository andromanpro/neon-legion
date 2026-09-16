#!/usr/bin/env python
"""Calendar-day re-estimation scaffold for #106-C.

Default dry-run mode is deterministic and makes no oracle calls. `--write`
invokes the existing Codex oracle path from `tracker/estimate-task.py`, which
can launch a subprocess and call out through the authenticated Codex CLI. Run
that mode only from the architect/host environment.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRACKER_DIR = PROJECT_ROOT / "tracker"
ESTIMATOR_PATH = TRACKER_DIR / "estimate-task.py"

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(TRACKER_DIR))
import summary  # noqa: E402


@dataclass(frozen=True)
class ChunkPlan:
    session_id: str
    task_key: str
    date: str
    transcript_path: Path
    events: tuple[dict, ...]
    read_at: str = ""  # when the transcript was read: the moment the input was fixed

    @property
    def event_count(self) -> int:
        return len(self.events)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan or write calendar-day task estimates for covered sessions."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="write",
        action="store_false",
        default=False,
        help="Print the chunk plan without oracle calls or writes (default).",
    )
    mode.add_argument(
        "--write",
        dest="write",
        action="store_true",
        help="Call the oracle and append chunk-keyed task entries.",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=3,
        help="Oracle samples per chunk in --write mode.",
    )
    parser.add_argument(
        "--limit-worst",
        type=int,
        default=0,
        help="Keep only the N most saturated sessions (saturation = total "
        "events / baseline-hours; grossly under-counted marathons score "
        "highest). 0 = no limit.",
    )
    parser.add_argument(
        "--multi-day-only",
        action="store_true",
        help="Only plan sessions spanning >=2 calendar days. Single-day "
        "sessions are a no-op under chunk-mode (1 chunk == the session "
        "fallback), so re-estimating them adds oracle noise for no "
        "structural gain — this is the intended #106-C scope.",
    )
    return parser.parse_args(argv)


def load_estimator_module():
    spec = importlib.util.spec_from_file_location("estimate_task", ESTIMATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load estimator module from {ESTIMATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def is_chunk_task_key(key: str) -> bool:
    if not isinstance(key, str):
        return False
    _, sep, suffix = key.rpartition(":")
    if not sep:
        return False
    try:
        datetime.strptime(suffix, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def resolve_transcript_path(value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        return path if path.exists() and path.is_file() else None
    except OSError:
        return None


def transcript_chunks(transcript_path: Path, estimator=None) -> dict[str, list[dict]]:
    """Events per local calendar day, split by estimate-task.py::read_day_chunks.

    The tool used to split days itself with summary.parse_event_ts, which keeps an
    offset-less timestamp on its own date while the day loop reads it as UTC, so
    one date could get different events here and in the loop (1c re-audit, P1).
    """
    estimator = estimator or load_estimator_module()
    return {
        day: [event for event, _ts in pairs]
        for day, pairs in estimator.read_day_chunks(str(transcript_path)).items()
    }


def covered_session_plans(
    tasks: dict, multi_day_only: bool = False, estimator=None
) -> tuple[list[ChunkPlan], dict[str, int]]:
    estimator = estimator or load_estimator_module()
    stats = {
        "sessions_seen": 0,
        "covered_sessions": 0,
        "missing_transcript": 0,
        "unreadable_transcript": 0,
        "single_day_skipped": 0,
        "chunks_planned": 0,
        "chunks_existing": 0,
    }
    plans: list[ChunkPlan] = []

    for session_id, entry in sorted(tasks.items()):
        if not isinstance(session_id, str) or is_chunk_task_key(session_id):
            continue
        stats["sessions_seen"] += 1
        if not isinstance(entry, dict) or summary.effective_task_hours(entry) is None:
            continue
        stats["covered_sessions"] += 1
        transcript_path = resolve_transcript_path(entry.get("transcript_path"))
        if transcript_path is None:
            stats["missing_transcript"] += 1
            continue
        read_at = estimator.input_read_time()
        try:
            chunks = transcript_chunks(transcript_path, estimator)
        except (OSError, UnicodeDecodeError):
            stats["unreadable_transcript"] += 1
            continue

        if multi_day_only and len(chunks) < 2:
            stats["single_day_skipped"] += 1
            continue

        for date_key, events in sorted(chunks.items()):
            task_key = f"{session_id}:{date_key}"
            if task_key in tasks:
                stats["chunks_existing"] += 1
            plans.append(
                ChunkPlan(
                    session_id=session_id,
                    task_key=task_key,
                    date=date_key,
                    transcript_path=transcript_path,
                    events=tuple(events),
                    read_at=read_at,
                )
            )
            stats["chunks_planned"] += 1

    return plans, stats


def build_chunk_prompt(estimator, plan: ChunkPlan) -> str:
    """The day loop's prompt for this day.

    estimate-task.py::build_day_input is the only reader of a day's input. This
    tool used to keep its own Claude-only copy, which dropped Codex conversation,
    zeroed its counters and passed compaction summaries as prompts (1c audit, P1).
    """
    pairs = []
    for event in plan.events:
        ts = estimator.parse_transcript_ts(event.get("timestamp") or event.get("ts"))
        if ts is not None:
            pairs.append((event, ts))
    _, _, metrics, context = estimator.build_day_input(pairs)
    return estimator.build_estimation_prompt(context, metrics)


def median_entry(entries: list[dict]) -> dict:
    if not entries:
        raise ValueError("no oracle samples")
    ordered = sorted(entries, key=lambda item: float(item.get("ai_baseline_hours") or 0.0))
    return dict(ordered[(len(ordered) - 1) // 2])


def estimate_chunk(estimator, plan: ChunkPlan, samples: int) -> dict:
    prompt = build_chunk_prompt(estimator, plan)
    entries = [estimator.run_oracle(prompt) for _ in range(samples)]
    entry = median_entry(entries)
    entry["transcript_path"] = str(plan.transcript_path)
    entry["source_session_id"] = plan.session_id
    entry["chunk_date"] = plan.date
    entry["chunk_event_count"] = plan.event_count
    entry["estimation_mode"] = "calendar-day-chunk"
    entry["day_input_version"] = estimator.DAY_INPUT_VERSION
    # Only a time taken before the transcript was read says what the input saw;
    # stamping now (after the oracle answered) would repeat the midnight bug.
    if plan.read_at:
        entry["day_input_read_at"] = plan.read_at
    entry["sample_count"] = len(entries)
    entry["sample_hours"] = [
        float(sample.get("ai_baseline_hours") or 0.0)
        for sample in entries
    ]
    return entry


def print_plan(plans: list[ChunkPlan], tasks: dict) -> None:
    print("session_id\tdate\tevent_count\twould_call")
    for plan in plans:
        would_call = "no-existing" if plan.task_key in tasks else "yes"
        print(f"{plan.session_id}\t{plan.date}\t{plan.event_count}\t{would_call}")


def print_summary(stats: dict, written: int, skipped_existing: int, failed: int) -> None:
    print()
    print("summary")
    for key in (
        "sessions_seen",
        "covered_sessions",
        "missing_transcript",
        "unreadable_transcript",
        "single_day_skipped",
        "chunks_planned",
        "chunks_existing",
    ):
        print(f"{key}\t{stats.get(key, 0)}")
    print(f"chunks_written\t{written}")
    print(f"chunks_skipped_existing\t{skipped_existing}")
    print(f"chunks_failed\t{failed}")


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.samples < 1:
        raise SystemExit("--samples must be >= 1")

    tasks = summary.read_tasks()
    estimator = load_estimator_module()
    plans, stats = covered_session_plans(tasks, multi_day_only=args.multi_day_only, estimator=estimator)

    if args.limit_worst and args.limit_worst > 0:
        by_session: dict[str, list[ChunkPlan]] = {}
        for plan in plans:
            by_session.setdefault(plan.session_id, []).append(plan)

        def saturation(session_id: str) -> float:
            total_events = sum(p.event_count for p in by_session[session_id])
            entry = tasks.get(session_id)
            base = summary.effective_task_hours(entry) if isinstance(entry, dict) else None
            return total_events / max(float(base or 0.0), 0.1)

        worst = sorted(by_session, key=saturation, reverse=True)[: args.limit_worst]
        worst_set = set(worst)
        stats["sessions_dropped_by_limit"] = len(by_session) - len(worst_set)
        plans = [p for p in plans if p.session_id in worst_set]
        stats["chunks_planned"] = len(plans)
        stats["chunks_existing"] = sum(1 for p in plans if p.task_key in tasks)
        print("# top saturated sessions kept (session_id\tsaturation ev/base-h):")
        for sid in worst:
            print(f"#   {sid}\t{saturation(sid):.1f}")

    print_plan(plans, tasks)

    if not args.write:
        print_summary(stats, written=0, skipped_existing=stats["chunks_existing"], failed=0)
        return 0

    written = 0
    skipped_existing = 0
    failed = 0
    for plan in plans:
        if plan.task_key in tasks:
            skipped_existing += 1
            continue
        try:
            entry = estimate_chunk(estimator, plan, args.samples)
            estimator.update_task_entry(plan.task_key, entry)
            tasks[plan.task_key] = entry
            written += 1
        except Exception as exc:
            failed += 1
            print(f"failed\t{plan.session_id}\t{plan.date}\t{exc}", file=sys.stderr)

    print_summary(stats, written=written, skipped_existing=skipped_existing, failed=failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
