#!/usr/bin/env python
"""Runs one task estimator under a hard deadline and always releases its lock.

SessionStart used to `Popen` the estimator directly and walk away. Nothing
then owned the child: the per-session lock aged out after ten minutes and
was deleted whether or not the estimator was still running, so a stuck run
kept burning while a second one started beside it. On 2026-08-01 that ended
with 247 live processes.

The fix is a process that stays: this supervisor owns the `Popen`, waits on
a deadline, kills the whole child tree if the deadline passes, and drops
the lock in a `finally`. It is itself short-lived and bounded, so the hook
can still fire and forget.

Usage: estimate-supervisor.py <session_id> <transcript_path> <deadline_seconds>
"""
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "tracker" / ".estimation-logs"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def load_hook_module():
    """Reuse the Stop hook's ops channel and liveness probe rather than
    growing a third copy of either."""
    import importlib.util

    hook_path = PROJECT_ROOT / "hooks" / "claude-track-calls.py"
    spec = importlib.util.spec_from_file_location("claude_track_calls", hook_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import the tracking hook from {hook_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOOK = load_hook_module()
COMPONENT = "estimate-supervisor"


def estimator_command(session_id: str, transcript_path: str) -> list[str]:
    """The child this supervisor owns. A seam: the deadline and the lock
    handling are what need testing, and they should not need a live
    `codex exec` to be exercised."""
    return ["py", "-3.14", "tracker/estimate-task.py", session_id, transcript_path]


def lock_path(session_id: str) -> Path:
    return LOG_DIR / f"{session_id}.lock"


def claim_lock(session_id: str) -> None:
    """Take ownership of the lock the hook created, so a later reaper sees
    the process that is actually working rather than the hook that has long
    since exited."""
    try:
        lock_path(session_id).write_text(str(os.getpid()), encoding="ascii")
    except OSError:
        pass


def release_lock(session_id: str) -> None:
    try:
        lock_path(session_id).unlink()
    except OSError:
        pass


def kill_tree(process: subprocess.Popen) -> None:
    """Kill the estimator and everything it started.

    The estimator shells out to `codex exec`; killing only the direct child
    would leave that behind, which is the leak this whole module exists to
    close.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            process.kill()
    else:
        try:
            os.killpg(os.getpgid(process.pid), 9)
        except (OSError, ProcessLookupError):
            process.kill()


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2

    session_id, transcript_path, deadline_raw = argv
    try:
        deadline = float(deadline_raw)
    except ValueError:
        return 2

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    claim_lock(session_id)
    log_path = LOG_DIR / f"{session_id}.log"
    command = estimator_command(session_id, transcript_path)

    popen_kwargs: dict = {
        "cwd": str(PROJECT_ROOT),
        "stdin": subprocess.DEVNULL,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    process = None
    try:
        with log_path.open("a", encoding="utf-8", newline="\n") as log_file:
            process = subprocess.Popen(command, stdout=log_file, **popen_kwargs)
            try:
                code = process.wait(timeout=deadline)
            except subprocess.TimeoutExpired:
                kill_tree(process)
                process.wait(timeout=30)
                HOOK.record_ops(
                    "estimator_deadline_exceeded",
                    f"session={session_id} pid={process.pid} deadline_s={deadline:.0f}",
                    component=COMPONENT,
                )
                return 1
        if code != 0:
            HOOK.record_ops(
                "estimator_failed",
                f"session={session_id} exit={code}",
                component=COMPONENT,
            )
        return 0
    except Exception as exc:
        HOOK.record_ops(
            "supervisor_failed",
            f"session={session_id} {type(exc).__name__}: {exc}",
            component=COMPONENT,
        )
        if process is not None and process.poll() is None:
            kill_tree(process)
        return 1
    finally:
        # The lock exists to stop a second estimator for this session. Once
        # this one is over — finished, killed or crashed — it must go, or the
        # session is never estimated again.
        release_lock(session_id)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
