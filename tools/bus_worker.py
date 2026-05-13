#!/usr/bin/env python
"""Long-running poll worker for the Phase 1.5 Git bus.

This module performs outbound HTTP calls through tools.bus_gitea.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
CLAIM_RE = re.compile(
    r"<!--\s*neon-claim:v1\s+host=\S+\s+exec=(?P<exec>\S+)\s+"
    r"claimed_at=\S+\s+lease_seconds=\d+\s*-->"
)

# DeepSeek audit A1: payload reads MUST be confined to an allowlisted root.
# Without this, a malicious envelope with payload_ref=file:///C:/Users/.gitea-token
# turns the worker into an arbitrary-file-read oracle (the sha256 mismatch comment
# echoes the actual sha of the file, leaking content fingerprints).
PAYLOAD_ROOT_ENV = "BUS_PAYLOAD_ROOT"

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
    # Step 1: optimistic label swap. Gitea does not provide CAS for labels, so
    # a later claim-comment verification decides the canonical winner.
    try:
        claimed = _set_state(number, labels, CLAIMED)
    except BusGiteaError as exc:
        log(f"#{number} claim PATCH failed: {exc}; skipping", level="error")
        return
    claimed_labels = _label_names(claimed)
    if CLAIMED not in claimed_labels:
        log(f"#{number} claim PATCH did not stick; skipping")
        return

    # Step 2: post claim-comment carrying our exec_id.
    try:
        my_claim_comment = bus_gitea.comment(number, _claim_comment(host, exec_id, lease_seconds))
    except BusGiteaError as exc:
        log(f"#{number} claim comment POST failed: {exc}; skipping (lease will expire)", level="error")
        return

    # Step 3: poor-man's CAS via monotonic issue-comment IDs.
    if not _verify_claim_won(number, exec_id, my_claim_comment.get("id")):
        log(f"#{number} lost claim race to a concurrent worker; releasing")
        return

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

        result = {"status": "done", "result": handler_result}
        terminal_state = DONE
    except _WorkerFailure as exc:
        _stop_heartbeat(heartbeat_done, heartbeat_thread)
        result = {"status": "failed", "reason": exc.reason, **exc.details}
        terminal_state = FAILED
    except Exception as exc:
        _stop_heartbeat(heartbeat_done, heartbeat_thread)
        result = {
            "status": "failed",
            "reason": "handler_exception",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        terminal_state = FAILED

    # DeepSeek audit C1: single finalise point — result is posted exactly once,
    # state transition is wrapped so a transient Gitea 5xx/4xx at the very end
    # cannot push the issue into a contradictory state (don/fail double-posted).
    # On finalise failure the result is still in the issue; the reaper will
    # expire the issue and a future poll picks it up cleanly.
    _post_result(number, exec_id, result)
    try:
        _set_state(number, current_labels, terminal_state, close=True)
    except BusGiteaError as exc:
        log(
            f"#{number} orphaned: result={result['status']} but {terminal_state} "
            f"transition failed: {exc}",
            level="error",
        )


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


def _verify_claim_won(issue_number: int, my_exec_id: str, my_comment_id: int | None) -> bool:
    """Confirm our claim-comment is the *earliest* (lowest-id) on the issue.

    DeepSeek audit on PR #71 found the original "highest id wins" inverts the
    CAS contract: under an interleaved POST→verify pattern, both racing workers
    can pass the verify step. The first poster's verify (only its own comment
    visible) sees its id as the highest and proceeds. The second poster's
    verify (both comments visible) also sees its own id as the highest and
    proceeds. Both run the handler.

    Lowest id wins flips that: only the worker whose comment carries the
    smallest neon-claim:v1 id among all such comments is the canonical winner.
    The runner-up's verify sees the earlier comment and steps down.

    Both `my_exec_id` AND `my_comment_id` must match — guards against stale
    `neon-claim:v1` comments from a prior lease cycle that the reaper hasn't
    cleaned yet (otherwise an old A-comment from cycle 1 could mask A's new
    comment in cycle 2 if A reuses the host).
    """
    try:
        comments = bus_gitea.list_comments(issue_number)
    except BusGiteaError as exc:
        log(f"#{issue_number} claim verify list_comments failed: {exc}; assuming lost", level="error")
        return False

    lowest_id = None
    lowest_exec = None
    for comment in comments:
        match = CLAIM_RE.search(comment.get("body") or "")
        if not match:
            continue
        comment_id = comment.get("id")
        if comment_id is None:
            continue
        if lowest_id is None or comment_id < lowest_id:
            lowest_id = comment_id
            lowest_exec = match.group("exec")

    won = (
        lowest_exec == my_exec_id
        and my_comment_id is not None
        and lowest_id == my_comment_id
    )
    if not won:
        log(
            f"#{issue_number} claim lost: lowest_exec={lowest_exec or '<none>'} "
            f"lowest_id={lowest_id} my_exec={my_exec_id} my_comment_id={my_comment_id}"
        )
    return won


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
        # DeepSeek audit A1: do NOT echo the actual sha — that turns the worker
        # into a file-content fingerprint oracle. Only the expected value is
        # safe to surface (the issuer already knows it).
        raise _WorkerFailure("payload_sha_mismatch", expected=expected)
    if not isinstance(payload, dict):
        raise _WorkerFailure("invalid_payload_json", error="payload root must be an object")
    return payload


def _payload_read(payload_ref: str):
    path = _payload_path(payload_ref)
    return path.read_bytes()


def _payload_root() -> Path | None:
    raw = os.environ.get(PAYLOAD_ROOT_ENV)
    if not raw:
        return None
    return Path(raw).resolve()


def _payload_path(payload_ref: str) -> Path:
    parsed = urllib.parse.urlparse(payload_ref)
    if parsed.scheme == "smb" and parsed.netloc:
        raw_path = f"//{parsed.netloc}{parsed.path}"
    else:
        raw_path = parsed.path if parsed.scheme else payload_ref
    raw_path = urllib.parse.unquote(raw_path)
    if os.name == "nt" and len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
        raw_path = raw_path[1:]
    candidate = Path(raw_path).resolve()

    # DeepSeek audit A1: confine reads to BUS_PAYLOAD_ROOT to prevent path
    # traversal (file:///C:/Users/.gitea-token, smb://host/../etc/passwd, etc.).
    # If the env var is unset, refuse all reads — operators MUST opt in
    # explicitly by pointing at a payload directory.
    root = _payload_root()
    if root is None:
        raise _WorkerFailure(
            "payload_root_unset",
            hint=f"set {PAYLOAD_ROOT_ENV} to a directory containing task payloads",
        )
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise _WorkerFailure(
            "payload_outside_root",
            root=str(root),
        ) from exc
    return candidate


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
