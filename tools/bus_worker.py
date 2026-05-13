#!/usr/bin/env python
"""Long-running poll worker for the Phase 1.5 Git bus.

This module performs outbound HTTP calls through tools.bus_gitea.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import signal
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import bus_envelope, bus_gitea  # noqa: E402
from tools.bus_gitea import BusGiteaError  # noqa: E402


PHASE_LABEL = "phase:1.5-git-bus"
STATE_PREFIX = "neon:state/"
PENDING = "neon:state/pending"
CLAIMED = "neon:state/claimed"
IN_PROGRESS = "neon:state/in-progress"
DONE = "neon:state/done"
FAILED = "neon:state/failed"
HTTP_SCHEMES = {"http", "https"}
LOCAL_SCHEMES = {"", "file", "smb"}

_STOP = threading.Event()


def echo_handler(envelope: dict, payload: dict) -> dict:
    """Returns the payload unchanged. Smoke handler for the worker itself."""
    return {"echo": payload}


HANDLERS: dict[str, Callable[[dict, dict], dict]] = {"echo": echo_handler}


class _WorkerFailure(Exception):
    def __init__(self, reason: str, **details):
        super().__init__(reason)
        self.reason = reason
        self.details = details


def register_handler(kind: str, handler) -> None:
    HANDLERS[kind] = handler


def run(host: str, poll_interval: int = 30) -> None:
    while not _STOP.is_set():
        try:
            issues = bus_gitea.list_issues(
                state="open",
                labels=[PHASE_LABEL, PENDING, f"neon:target/{host}"],
            )
        except BusGiteaError as exc:
            log(f"poll failed: {exc}", level="error")
            issues = []

        for issue in sorted(issues, key=lambda i: i["number"]):
            if _STOP.is_set():
                break
            try:
                process_issue(issue, host)
            except Exception as exc:  # keep the poller alive on one bad issue
                log(f"unhandled exception on #{issue['number']}: {exc}", level="error")

        _wait_or_stop(poll_interval)


def process_issue(issue: dict, host: str) -> None:
    number = issue["number"]
    envelope = bus_envelope.parse(issue.get("body") or "")
    if envelope is None:
        log(f"#{number} has no valid neon-task envelope; skipping")
        return

    labels = _label_names(issue)
    if PENDING not in labels:
        log(f"#{number} is no longer pending; skipping")
        return

    exec_id = _new_exec_id(host)
    lease_seconds = int(envelope["lease_seconds"])
    try:
        claimed = _set_state(number, labels, CLAIMED)
    except BusGiteaError as exc:
        log(f"#{number} claim failed: {exc}; skipping", level="error")
        return
    claimed_labels = _label_names(claimed)
    if CLAIMED not in claimed_labels:
        log(f"#{number} claim did not stick; skipping")
        return

    bus_gitea.comment(number, _claim_comment(host, exec_id, lease_seconds))
    current_labels = claimed_labels
    heartbeat_done = None
    heartbeat_thread = None

    try:
        payload = _load_payload(envelope)
        current_labels = _label_names(_set_state(number, current_labels, IN_PROGRESS))
        handler = HANDLERS.get(envelope["kind"])
        if handler is None:
            raise _WorkerFailure("unknown_kind", kind=envelope["kind"])

        heartbeat_done, heartbeat_thread = _start_heartbeat(number, exec_id, lease_seconds)
        handler_result = handler(envelope, payload)
        _stop_heartbeat(heartbeat_done, heartbeat_thread)
        heartbeat_done = heartbeat_thread = None

        _post_result(number, exec_id, {"status": "done", "result": handler_result})
        _set_state(number, current_labels, DONE, close=True)
    except _WorkerFailure as exc:
        _stop_heartbeat(heartbeat_done, heartbeat_thread)
        result = {"status": "failed", "reason": exc.reason, **exc.details}
        _post_result(number, exec_id, result)
        _set_state(number, current_labels, FAILED, close=True)
    except Exception as exc:
        _stop_heartbeat(heartbeat_done, heartbeat_thread)
        result = {
            "status": "failed",
            "reason": "handler_exception",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        _post_result(number, exec_id, result)
        _set_state(number, current_labels, FAILED, close=True)


def log(message: str, *, level: str = "info") -> None:
    prefix = "[bus-worker]"
    if level != "info":
        prefix = f"{prefix} {level.upper()}"
    print(f"{prefix} {message}", file=sys.stderr)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Worker host name to claim neon:target/<host> tasks.")
    parser.add_argument("--poll-interval", type=int, default=30, help="Seconds between issue polls.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    signal.signal(signal.SIGINT, _request_stop)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _request_stop)
        except (OSError, ValueError):
            pass

    args = parse_args(sys.argv[1:] if argv is None else argv)
    run(args.host, max(1, args.poll_interval))
    return 0


def _request_stop(_signum, _frame) -> None:
    _STOP.set()


def _new_exec_id(host: str) -> str:
    return f"{host}-{int(time.time())}-{secrets.token_hex(3)}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _claim_comment(host: str, exec_id: str, lease_seconds: int) -> str:
    return (
        f"<!-- neon-claim:v1 host={host} exec={exec_id} "
        f"claimed_at={_now_iso()} lease_seconds={lease_seconds} -->"
    )


def _heartbeat_comment(exec_id: str) -> str:
    return f"<!-- neon-hb:v1 exec={exec_id} ts={_now_iso()} -->"


def _post_result(number: int, exec_id: str, result: dict) -> None:
    status = result["status"]
    body = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    bus_gitea.comment(number, f"<!-- neon-result:v1 exec={exec_id} status={status} -->\n{body}\n<!-- /neon-result:v1 -->")


def _start_heartbeat(number: int, exec_id: str, lease_seconds: int) -> tuple[threading.Event, threading.Thread]:
    done = threading.Event()
    thread = threading.Thread(
        target=_heartbeat_loop,
        args=(number, exec_id, lease_seconds, done),
        daemon=True,
    )
    thread.start()
    return done, thread


def _heartbeat_loop(number: int, exec_id: str, lease_seconds: int, done: threading.Event) -> None:
    interval = max(1, lease_seconds // 3)
    while not _STOP.is_set():
        if done.wait(timeout=interval):
            break
        try:
            bus_gitea.comment(number, _heartbeat_comment(exec_id))
        except BusGiteaError as exc:
            log(f"heartbeat failed on #{number}: {exc}", level="error")


def _stop_heartbeat(done: threading.Event | None, thread: threading.Thread | None) -> None:
    if done is None or thread is None:
        return
    done.set()
    thread.join(timeout=2)


def _load_payload(envelope: dict) -> dict:
    payload_ref = envelope["payload_ref"]
    scheme = urllib.parse.urlparse(payload_ref).scheme.lower()
    if scheme in HTTP_SCHEMES:
        raise _WorkerFailure("unsupported_payload_scheme", scheme=scheme)
    if scheme not in LOCAL_SCHEMES:
        raise _WorkerFailure("unsupported_payload_scheme", scheme=scheme or "<empty>")

    raw_or_payload = _payload_read(payload_ref)
    if isinstance(raw_or_payload, bytes):
        raw = raw_or_payload
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _WorkerFailure("invalid_payload_json", error=str(exc)) from exc
    else:
        payload = raw_or_payload
        raw = _canonical_bytes(payload)

    actual = hashlib.sha256(raw).hexdigest()
    expected = envelope["payload_sha256"].lower()
    if actual.lower() != expected:
        raise _WorkerFailure("payload_sha_mismatch", expected=expected, actual=actual)
    if not isinstance(payload, dict):
        raise _WorkerFailure("invalid_payload_json", error="payload root must be an object")
    return payload


def _payload_read(payload_ref: str):
    path = _payload_path(payload_ref)
    return path.read_bytes()


def _payload_path(payload_ref: str) -> Path:
    parsed = urllib.parse.urlparse(payload_ref)
    if parsed.scheme == "smb" and parsed.netloc:
        raw_path = f"//{parsed.netloc}{parsed.path}"
    else:
        raw_path = parsed.path if parsed.scheme else payload_ref
    raw_path = urllib.parse.unquote(raw_path)
    if os.name == "nt" and len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
        raw_path = raw_path[1:]
    return Path(raw_path)


def _canonical_bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _set_state(number: int, labels: list[str], state_label: str, *, close: bool = False) -> dict:
    next_labels = _replace_state_label(labels, state_label)
    kwargs = {"labels": next_labels}
    if close:
        kwargs["state"] = "closed"
    return bus_gitea.update_issue(number, **kwargs)


def _replace_state_label(labels: list[str], state_label: str) -> list[str]:
    kept = [label for label in labels if not label.startswith(STATE_PREFIX)]
    return [*kept, state_label]


def _label_names(issue_or_labels) -> list[str]:
    labels = issue_or_labels.get("labels", []) if isinstance(issue_or_labels, dict) else issue_or_labels
    names = []
    for label in labels or []:
        if isinstance(label, str):
            names.append(label)
        elif isinstance(label, dict) and isinstance(label.get("name"), str):
            names.append(label["name"])
    return names


def _wait_or_stop(seconds: int | float) -> None:
    slept = 0.0
    interval = max(0.0, float(seconds))
    while slept < interval and not _STOP.is_set():
        step = min(1.0, interval - slept)
        time.sleep(step)
        slept += step


if __name__ == "__main__":
    raise SystemExit(main())
