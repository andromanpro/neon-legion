#!/usr/bin/env python
import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRACKER_DIR = PROJECT_ROOT / "tracker"
EVENTS_FILE = TRACKER_DIR / "claude-events.jsonl"
TASKS_FILE = TRACKER_DIR / "tasks.json"
TASKS_LOCK_FILE = TRACKER_DIR / ".tasks.lock"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review or override Claude task complexity estimates.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--session-id", help="Session ID to annotate with --hours.")
    action.add_argument("--list", dest="list_entries", action="store_true", help="List all task estimates.")
    action.add_argument("--pending", action="store_true", help="List event session IDs missing from tasks.json.")
    action.add_argument("--reestimate", metavar="SESSION_ID", help="Delete an estimate so the hook can re-run it.")
    parser.add_argument("--hours", type=float, help="Manual human-hours-without-AI override.")
    parser.add_argument("--description", help="Optional one-line description override.")
    return parser.parse_args(argv)


def acquire_tasks_lock(timeout_seconds: int = 10) -> int | None:
    TRACKER_DIR.mkdir(parents=True, exist_ok=True)
    deadline = datetime.now() + timedelta(seconds=timeout_seconds)
    while True:
        try:
            fd = os.open(str(TASKS_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            return fd
        except FileExistsError:
            if datetime.now() >= deadline:
                return None
            time.sleep(0.05)
        except OSError:
            return None


def release_tasks_lock(fd: int | None) -> None:
    if fd is None:
        return
    os.close(fd)
    try:
        TASKS_LOCK_FILE.unlink()
    except OSError:
        pass


def atomic_write_json(path: Path, data: dict) -> None:
    TRACKER_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with temp_path.open("w", encoding="utf-8", newline="\n") as target:
        json.dump(data, target, ensure_ascii=False, indent=2, sort_keys=True)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
    os.replace(temp_path, path)


def read_tasks() -> dict:
    if not TASKS_FILE.exists():
        return {}
    try:
        with TASKS_FILE.open("r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def ensure_tasks_file() -> None:
    if TASKS_FILE.exists():
        return
    fd = acquire_tasks_lock()
    try:
        if not TASKS_FILE.exists():
            atomic_write_json(TASKS_FILE, {})
    finally:
        release_tasks_lock(fd)


def update_tasks(mutator) -> dict:
    fd = acquire_tasks_lock()
    try:
        tasks = read_tasks()
        mutator(tasks)
        atomic_write_json(TASKS_FILE, tasks)
        return tasks
    finally:
        release_tasks_lock(fd)


def read_event_session_ids() -> list[str]:
    if not EVENTS_FILE.exists():
        return []

    seen = set()
    session_ids = []
    with EVENTS_FILE.open("r", encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            session_id = event.get("session_id")
            if isinstance(session_id, str) and session_id and session_id not in seen:
                seen.add(session_id)
                session_ids.append(session_id)
    return session_ids


def find_transcript(session_id: str) -> str:
    pattern = str(Path.home() / ".claude" / "projects" / "*" / f"{session_id}.jsonl")
    matches = [Path(path) for path in glob.glob(pattern)]
    matches = [path for path in matches if path.is_file()]
    if not matches:
        return ""
    return str(max(matches, key=lambda path: path.stat().st_mtime))


def effective_hours(entry: dict) -> object:
    corrected = entry.get("human_corrected_hours")
    if corrected is not None:
        return corrected
    return entry.get("ai_baseline_hours")


def fmt_hours(value: object) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return ""


def markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def list_entries(tasks: dict) -> None:
    print("| Session ID (short) | Description | AI baseline (h) | Human corrected (h) | Effective (h) |")
    print("|---|---|---:|---:|---:|")
    for session_id, entry in sorted(tasks.items()):
        if not isinstance(entry, dict):
            continue
        print(
            "| "
            + " | ".join(
                [
                    markdown_cell(session_id[:8]),
                    markdown_cell(entry.get("brief_description", "")),
                    fmt_hours(entry.get("ai_baseline_hours")),
                    fmt_hours(entry.get("human_corrected_hours")),
                    fmt_hours(effective_hours(entry)),
                ]
            )
            + " |"
        )


def list_pending(tasks: dict) -> None:
    task_ids = set(tasks)
    for session_id in read_event_session_ids():
        if session_id not in task_ids:
            print(session_id)


def apply_override(session_id: str, hours: float, description: str | None) -> None:
    if hours < 0:
        raise ValueError("--hours must be non-negative")

    def mutate(tasks: dict) -> None:
        entry = tasks.get(session_id)
        if not isinstance(entry, dict):
            entry = {
                "ai_baseline_hours": None,
                "human_corrected_hours": None,
                "brief_description": "",
                "estimated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "estimation_confidence": "low",
                "needs_manual_review": False,
                "transcript_path": find_transcript(session_id),
            }

        entry["human_corrected_hours"] = hours
        if description is not None:
            entry["brief_description"] = " ".join(description.strip().split())
        entry["estimated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        entry["needs_manual_review"] = False
        tasks[session_id] = entry

    update_tasks(mutate)
    print(f"updated {session_id}")


def reestimate(session_id: str) -> None:
    removed = False

    def mutate(tasks: dict) -> None:
        nonlocal removed
        removed = session_id in tasks
        tasks.pop(session_id, None)

    update_tasks(mutate)
    if removed:
        print(f"removed {session_id}")
    else:
        print(f"not found {session_id}")


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    ensure_tasks_file()
    tasks = read_tasks()

    try:
        if args.list_entries:
            list_entries(tasks)
            return 0
        if args.pending:
            list_pending(tasks)
            return 0
        if args.reestimate:
            reestimate(args.reestimate)
            return 0

        if args.session_id:
            if args.hours is None:
                print("--hours is required with --session-id", file=sys.stderr)
                return 2
            apply_override(args.session_id, args.hours, args.description)
            return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
