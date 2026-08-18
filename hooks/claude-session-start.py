#!/usr/bin/env python
import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRACKER_DIR = PROJECT_ROOT / "tracker"
EVENTS_FILE = TRACKER_DIR / "claude-events.jsonl"
TASKS_FILE = TRACKER_DIR / "tasks.json"
TASKS_LOCK_FILE = TRACKER_DIR / ".tasks.lock"
LOG_DIR = TRACKER_DIR / ".estimation-logs"
RECENT_WINDOW = timedelta(hours=24)

# The supervisor owns the hard deadline; this TTL is only the backstop for a
# lock whose owning pid cannot be read at all, so it sits above the deadline.
ESTIMATOR_DEADLINE_SECONDS = 10 * 60
INFLIGHT_TTL_SECONDS = 15 * 60
MAX_DISPATCH_PER_FIRE = 5

# A ceiling on estimators alive at once, across sessions. MAX_DISPATCH_PER_FIRE
# bounds a single hook firing; it did nothing to stop N firings stacking up,
# which is how 2026-08-01 reached 247 processes.
MAX_LIVE_ESTIMATORS = 3

# read_recent_session_ids only needs the last 24 hours. Reading the whole
# ledger for that cost more with every event: it is past 278k lines / 110 MB,
# on a hook that runs before the user can type. Events are only ever appended,
# so anything recent is near the end.
LEDGER_TAIL_BYTES = 8 * 1024 * 1024


def load_hook_module():
    """Share the Stop hook's ops channel and liveness probe instead of
    keeping a second copy of either."""
    import importlib.util

    hook_path = PROJECT_ROOT / "hooks" / "claude-track-calls.py"
    spec = importlib.util.spec_from_file_location("claude_track_calls", hook_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import the tracking hook from {hook_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOOK = load_hook_module()
COMPONENT = "claude-session-start"

if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
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

    with EVENTS_FILE.open("rb") as raw:
        size = raw.seek(0, os.SEEK_END)
        raw.seek(max(0, size - LEDGER_TAIL_BYTES))
        tail = raw.read().decode("utf-8", errors="replace")

    lines = tail.splitlines()
    if size > LEDGER_TAIL_BYTES and lines:
        lines = lines[1:]  # the window starts mid-record

    for line in lines:
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
        if event.get("model") == "<synthetic>":
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


def lock_holder_is_gone(path: Path, ttl_seconds: int) -> bool:
    """Whether a lock file was left behind by a process that no longer runs.

    Same rule as the ledger lock, for the same reason: a bare O_EXCL lock
    whose owner died once disabled collection for six hours before anyone
    noticed. A live owner keeps its lock however long it works; the age cap
    only applies when there is no readable pid to ask about.
    """
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    try:
        holder = int(path.read_text(encoding="ascii").strip() or 0)
    except (OSError, ValueError):
        holder = 0
    if holder > 0:
        return not HOOK.process_alive(holder)
    return age >= ttl_seconds


def acquire_tasks_lock() -> int | None:
    """O_EXCL with one recovery attempt.

    This used to be a single bare attempt: a crash between acquire and
    release left the file forever, and every later manual-review write
    silently did nothing — swallowed by main()'s catch-all, with no signal.
    """
    for attempt in range(2):
        try:
            fd = os.open(str(TASKS_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            return fd
        except OSError:
            if attempt == 0 and lock_holder_is_gone(TASKS_LOCK_FILE, INFLIGHT_TTL_SECONDS):
                try:
                    TASKS_LOCK_FILE.unlink()
                except OSError:
                    return None
                HOOK.record_ops("tasks_lock_stale_cleared", str(TASKS_LOCK_FILE), component=COMPONENT)
                continue
            return None
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
    """Mark a session as un-estimatable, MERGING into any existing entry.

    This used to bail out on `session_id in tasks`, which was harmless only
    while the pending queue also keyed on entry presence. Now that the queue
    keys on whether an ESTIMATE exists (see needs_estimation), bailing here
    would be an infinite retry: a session with a rotated-away transcript would
    be re-dispatched on every single SessionStart and never get the flag that
    takes it out of the queue. Merge instead, and never clobber an estimate
    that some other path managed to produce meanwhile.
    """
    fd = acquire_tasks_lock()
    if fd is None:
        return

    try:
        tasks = read_tasks()
        entry = tasks.get(session_id)
        entry = dict(entry) if isinstance(entry, dict) else {}
        if entry.get("ai_baseline_hours") is not None:
            return  # already estimated by another path — leave it alone
        entry.update({
            "ai_baseline_hours": None,
            "human_corrected_hours": entry.get("human_corrected_hours"),
            "brief_description": description,
            "estimated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "estimation_confidence": "low",
            "needs_manual_review": True,
            "transcript_path": transcript_path or entry.get("transcript_path", ""),
        })
        tasks[session_id] = entry
        atomic_write_json(TASKS_FILE, tasks)
    finally:
        release_tasks_lock(fd)


def log_line(session_id: str, message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{session_id}.log"
    with log_path.open("a", encoding="utf-8", newline="\n") as target:
        target.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {message}\n")


def needs_estimation(entry) -> bool:
    """True when a session still has no baseline estimate.

    This used to be a bare `session_id not in tasks`, which silently disabled
    the estimator: backfill-sentiment-markers.py (runs on EVERY deploy) calls
    update_task_entry() to record profanity/appreciation counts, creating the
    tasks.json entry BEFORE the session is ever estimated. From then on the
    session "already exists" and never enters the pending queue again.
    Estimation stopped completely on 2026-08-01: 91 sessions ended up with an
    entry but no hours, so they were excluded from productivity on both sides
    (no baseline in the numerator, no attention in the denominator) — days of
    real work read as "nothing happened".

    Presence of the entry is therefore not the signal; presence of an ESTIMATE
    is. `needs_manual_review` counts as attempted — those failed estimation
    for their own reason and must not be retried on every session start.
    """
    if not isinstance(entry, dict):
        return True
    if entry.get("needs_manual_review"):
        return False
    return not entry.get("estimated_at") and entry.get("ai_baseline_hours") is None


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
        # The old rule was age alone: after ten minutes the lock was deleted
        # and a second estimator started, while the first one kept running
        # untouched. Ask whether the owner is alive instead — the supervisor
        # writes its own pid here and removes the file when it is done.
        if not lock_holder_is_gone(lock_path, INFLIGHT_TTL_SECONDS):
            return False
        try:
            lock_path.unlink()
        except OSError:
            return False
        HOOK.record_ops("estimator_lock_stale_cleared", f"session={session_id}", component=COMPONENT)

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


def live_estimator_count() -> int:
    """Estimators running right now, counted from their locks."""
    if not LOG_DIR.is_dir():
        return 0
    return sum(
        1 for lock in LOG_DIR.glob("*.lock")
        if not lock_holder_is_gone(lock, INFLIGHT_TTL_SECONDS)
    )


def dispatch_estimator(session_id: str, transcript_path: str) -> bool:
    """Start one supervised estimator. True when it was actually launched.

    The hook no longer launches the estimator itself. It launches a
    supervisor that owns the child, enforces a hard deadline and releases
    the lock in a `finally` — because a bare Popen from a hook has no owner
    at all, and a run that hangs used to survive its own lock expiring.
    """
    if not create_inflight_lock(session_id):
        return False

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{session_id}.log"
    command = [
        "py", "-3.14", "tracker/estimate-supervisor.py",
        session_id, transcript_path, str(ESTIMATOR_DEADLINE_SECONDS),
    ]

    popen_kwargs = {
        "cwd": str(PROJECT_ROOT),
        "stdin": subprocess.DEVNULL,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    # Still fire-and-forget — the hook has to return before the user can
    # type — but what it forgets is now a process with a deadline.
    try:
        with log_path.open("a", encoding="utf-8", newline="\n") as log_file:
            subprocess.Popen(command, stdout=log_file, **popen_kwargs)
        return True
    except OSError as exc:
        remove_inflight_lock(session_id)
        log_line(session_id, f"failed to dispatch estimator: {exc}")
        HOOK.record_ops("estimator_dispatch_failed", f"session={session_id} {exc}", component=COMPONENT)
        return False


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
        pending = sorted(
            session_id for session_id in recent_session_ids
            if needs_estimation(tasks.get(session_id))
        )
        if not pending:
            return 0

        # The cap used to be applied here, before anything checked which of
        # these were already running: five sessions with live estimators
        # filled the quota and nothing behind them ever advanced. Count what
        # actually starts instead, and stop at whichever ceiling comes first.
        budget = min(MAX_DISPATCH_PER_FIRE, max(0, MAX_LIVE_ESTIMATORS - live_estimator_count()))
        if budget <= 0:
            HOOK.record_ops(
                "estimator_cap_reached",
                f"live={live_estimator_count()} pending={len(pending)}",
                component=COMPONENT,
            )
            return 0

        started = 0
        for session_id in pending:
            if started >= budget:
                break
            transcript_path = find_transcript(session_id)
            if transcript_path is None:
                log_line(session_id, "transcript not found")
                write_manual_review_entry(session_id, "", "transcript not found")
                continue
            if dispatch_estimator(session_id, transcript_path):
                started += 1
    except Exception:
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
