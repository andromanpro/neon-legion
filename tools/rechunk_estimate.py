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
import json
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


def transcript_event_ts(event: dict) -> datetime | None:
    return summary.parse_event_ts(event.get("timestamp") or event.get("ts"))


def transcript_chunks(transcript_path: Path) -> dict[str, list[dict]]:
    chunks: dict[str, list[dict]] = {}
    with transcript_path.open("r", encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            ts = transcript_event_ts(event)
            if ts is None:
                continue
            chunks.setdefault(summary.chunk_date(ts), []).append(event)
    return chunks


def covered_session_plans(
    tasks: dict, multi_day_only: bool = False
) -> tuple[list[ChunkPlan], dict[str, int]]:
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
        try:
            chunks = transcript_chunks(transcript_path)
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
                )
            )
            stats["chunks_planned"] += 1

    return plans, stats


def chunk_messages(estimator, events: tuple[dict, ...]) -> tuple[list[str], list[str]]:
    user_messages: list[str] = []
    assistant_messages: list[str] = []
    for event in events:
        role = estimator.transcript_role(event)
        if role is None:
            continue
        text = estimator.transcript_text(event).strip()
        if not text:
            continue
        if role == "user":
            user_messages.append(text)
        elif role == "assistant":
            assistant_messages.append(text)
    return user_messages, assistant_messages


def compute_chunk_metrics(estimator, events: tuple[dict, ...]) -> dict:
    metrics = {
        "event_count": len(events),
        "user_message_count": 0,
        "assistant_message_count": 0,
        "tool_call_count": 0,
        "span_hours": 0.0,
        "active_hours": 0.0,
    }
    timestamps: list[datetime] = []

    for event in events:
        role = estimator.transcript_role(event)
        if role == "user":
            metrics["user_message_count"] += 1
        elif role == "assistant":
            metrics["assistant_message_count"] += 1
        metrics["tool_call_count"] += estimator._tool_call_count_for_event(event, role)

        ts = transcript_event_ts(event)
        if ts is not None:
            timestamps.append(ts)

    if timestamps:
        ordered = sorted(timestamps)
        metrics["span_hours"] = max((ordered[-1] - ordered[0]).total_seconds(), 0.0) / 3600
        metrics["active_hours"] = estimator.active_hours_for_timestamps(
            timestamps,
            getattr(estimator, "SESSION_SIZE_GAP_MINUTES", 2),
        )

    return metrics


def build_chunk_prompt(estimator, plan: ChunkPlan) -> str:
    user_messages, assistant_messages = chunk_messages(estimator, plan.events)
    context = estimator.build_truncated_context_from_messages(user_messages, assistant_messages)
    metrics = compute_chunk_metrics(estimator, plan.events)
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
    plans, stats = covered_session_plans(tasks, multi_day_only=args.multi_day_only)

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

    estimator = load_estimator_module()
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
