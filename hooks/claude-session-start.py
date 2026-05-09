#!/usr/bin/env python
import glob
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRACKER_DIR = PROJECT_ROOT / "tracker"
EVENTS_FILE = TRACKER_DIR / "claude-events.jsonl"
TASKS_FILE = TRACKER_DIR / "tasks.json"
TASKS_LOCK_FILE = TRACKER_DIR / ".tasks.lock"
LOG_DIR = TRACKER_DIR / ".estimation-logs"
RECENT_WINDOW = timedelta(hours=24)
INFLIGHT_TTL_SECONDS = 10 * 60

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def read_hook_input() -> dict | None:
    raw = sys.stdin.read()
    if not raw.strip():
        return None

    data = json.loads(raw)
    if not isinstance(data, dict):
        return None

    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None

    return data


def parse_event_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def read_recent_session_ids(new_session_id: str) -> set[str]:
    cutoff = datetime.now().astimezone() - RECENT_WINDOW
    session_ids: set[str] = set()

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
            if not isinstance(session_id, str) or not session_id or session_id == new_session_id:
                continue

            ts = parse_event_ts(event.get("ts"))
            if ts is not None and ts >= cutoff:
                session_ids.add(session_id)

    return session_ids


def read_tasks() -> dict:
    if not TASKS_FILE.exists():
        return {}
    try:
        with TASKS_FILE.open("r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def atomic_write_json(path: Path, data: dict) -> None:
    TRACKER_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with temp_path.open("w", encoding="utf-8", newline="\n") as target:
        json.dump(data, target, ensure_ascii=False, indent=2, sort_keys=True)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
    os.replace(temp_path, path)


def acquire_tasks_lock() -> int | None:
    try:
        fd = os.open(str(TASKS_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        return fd
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


def write_manual_review_entry(session_id: str, transcript_path: str, description: str) -> None:
    fd = acquire_tasks_lock()
    if fd is None:
        return

    try:
        tasks = read_tasks()
        if session_id in tasks:
            return
        tasks[session_id] = {
            "ai_baseline_hours": None,
            "human_corrected_hours": None,
            "brief_description": description,
            "estimated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "estimation_confidence": "low",
            "needs_manual_review": True,
            "transcript_path": transcript_path,
        }
        atomic_write_json(TASKS_FILE, tasks)
    finally:
        release_tasks_lock(fd)


def log_line(session_id: str, message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{session_id}.log"
    with log_path.open("a", encoding="utf-8", newline="\n") as target:
        target.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {message}\n")


def find_transcript(session_id: str) -> str | None:
    pattern = str(Path.home() / ".claude" / "projects" / "*" / f"{session_id}.jsonl")
    matches = [Path(path) for path in glob.glob(pattern)]
    matches = [path for path in matches if path.is_file()]
    if not matches:
        return None
    return str(max(matches, key=lambda path: path.stat().st_mtime))


def create_inflight_lock(session_id: str) -> bool:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = LOG_DIR / f"{session_id}.lock"

    if lock_path.exists():
        age = datetime.now().timestamp() - lock_path.stat().st_mtime
        if age < INFLIGHT_TTL_SECONDS:
            return False
        try:
            lock_path.unlink()
        except OSError:
            return False

    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        return True
    except OSError:
        return False


def remove_inflight_lock(session_id: str) -> None:
    try:
        (LOG_DIR / f"{session_id}.lock").unlink()
    except OSError:
        pass


def dispatch_estimator(session_id: str, transcript_path: str) -> None:
    if not create_inflight_lock(session_id):
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{session_id}.log"
    command = ["py", "-3.14", "tracker/estimate-task.py", session_id, transcript_path]

    popen_kwargs = {
        "cwd": str(PROJECT_ROOT),
        "stdin": subprocess.DEVNULL,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    try:
        with log_path.open("a", encoding="utf-8", newline="\n") as log_file:
            subprocess.Popen(command, stdout=log_file, **popen_kwargs)
    except OSError as exc:
        remove_inflight_lock(session_id)
        log_line(session_id, f"failed to dispatch estimator: {exc}")


def main() -> int:
    try:
        hook_input = read_hook_input()
        if hook_input is None or not EVENTS_FILE.exists():
            return 0

        new_session_id = str(hook_input["session_id"])
        recent_session_ids = read_recent_session_ids(new_session_id)
        if not recent_session_ids:
            return 0

        tasks = read_tasks()
        pending = sorted(session_id for session_id in recent_session_ids if session_id not in tasks)
        if not pending:
            return 0

        for session_id in pending:
            transcript_path = find_transcript(session_id)
            if transcript_path is None:
                log_line(session_id, "transcript not found")
                write_manual_review_entry(session_id, "", "transcript not found")
                continue
            dispatch_estimator(session_id, transcript_path)
    except Exception:
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
