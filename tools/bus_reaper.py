#!/usr/bin/env python
"""Lease reaper for the Phase 1.5 Git bus.

This module performs outbound HTTP calls through tools.bus_gitea.
"""

from __future__ import annotations

import argparse
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import bus_gitea  # noqa: E402
from tools.bus_gitea import BusGiteaError  # noqa: E402


PHASE_LABEL = "phase:1.5-git-bus"
STATE_PREFIX = "neon:state/"
CLAIMED = "neon:state/claimed"
IN_PROGRESS = "neon:state/in-progress"
EXPIRED = "neon:state/expired"

CLAIM_RE = re.compile(
    r"<!--\s*neon-claim:v1\s+host=(\S+)\s+exec=(\S+)\s+"
    r"claimed_at=(\S+)\s+lease_seconds=(\d+)\s*-->"
)
HB_RE = re.compile(r"<!--\s*neon-hb:v1\s+exec=(\S+)\s+ts=(\S+)\s*-->")

_STOP = threading.Event()


def run(poll_interval: int = 60, lease_grace_multiplier: float = 1.5, *, once: bool = False) -> None:
    while not _STOP.is_set():
        for issue in _list_candidate_issues():
            if _STOP.is_set():
                break
            try:
                process_issue(issue, _now(), lease_grace_multiplier)
            except Exception as exc:  # keep the poller alive on one bad issue
                log(f"unhandled exception on #{issue['number']}: {exc}", level="error")
        if once:
            break
        _wait_or_stop(poll_interval)


def process_issue(issue: dict, now: datetime, lease_grace_multiplier: float = 1.5) -> None:
    number = issue["number"]
    labels = _label_names(issue)
    if EXPIRED in labels:
        log(f"#{number} is already expired; skipping")
        return

    comments = bus_gitea.list_comments(number)
    claim = _latest_claim(comments)
    if claim is None:
        log(f"#{number} has no neon-claim comment; skipping")
        return

    heartbeat_ts = _latest_heartbeat_ts(comments)
    reference_iso = heartbeat_ts or claim["claimed_at"]
    reference = _parse_iso(reference_iso)
    lease_seconds = int(claim["lease_seconds"])
    elapsed = (now - reference).total_seconds()
    if elapsed <= lease_seconds * lease_grace_multiplier:
        log(f"#{number} lease still fresh; elapsed={int(elapsed)}s lease={lease_seconds}s")
        return

    expire(issue, f"Worker stopped heartbeating after {int(elapsed)}s")


def expire(issue: dict, reason: str) -> None:
    number = issue["number"]
    labels = _label_names(issue)
    if EXPIRED in labels:
        log(f"#{number} is already expired; skipping")
        return

    next_labels = _replace_state_label(labels, EXPIRED)
    bus_gitea.update_issue(number, labels=next_labels)
    bus_gitea.comment(number, _expired_comment(reason))
    log(f"#{number} expired: {reason}")


def log(message: str, *, level: str = "info") -> None:
    prefix = "[bus-reaper]"
    if level != "info":
        prefix = f"{prefix} {level.upper()}"
    print(f"{prefix} {message}", file=sys.stderr)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--poll-interval", type=int, default=60, help="Seconds between issue polls.")
    parser.add_argument("--once", action="store_true", help="Run a single scan and exit.")
    parser.add_argument(
        "--lease-grace-multiplier",
        type=float,
        default=1.5,
        help="Expire after lease_seconds multiplied by this value.",
    )
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
    run(max(1, args.poll_interval), args.lease_grace_multiplier, once=args.once)
    return 0


def _request_stop(_signum, _frame) -> None:
    _STOP.set()


def _list_candidate_issues() -> list[dict]:
    try:
        issues = bus_gitea.list_issues(state="open", labels=[PHASE_LABEL])
    except BusGiteaError as exc:
        log(f"poll failed: {exc}", level="error")
        return []
    candidates = []
    for issue in issues:
        labels = _label_names(issue)
        if CLAIMED in labels or IN_PROGRESS in labels:
            candidates.append(issue)
    return sorted(candidates, key=lambda issue: issue["number"])


def _latest_claim(comments: list[dict]) -> dict | None:
    latest = None
    for comment in comments:
        match = CLAIM_RE.search(comment.get("body") or "")
        if match:
            latest = {
                "host": match.group(1),
                "exec": match.group(2),
                "claimed_at": match.group(3),
                "lease_seconds": match.group(4),
            }
    return latest


def _latest_heartbeat_ts(comments: list[dict]) -> str | None:
    latest = None
    for comment in comments:
        match = HB_RE.search(comment.get("body") or "")
        if match and (latest is None or match.group(2) > latest):
            latest = match.group(2)
    return latest


def _expired_comment(reason: str) -> str:
    return f"<!-- neon-expired:v1 by=reaper at={_now_iso()} -->\n{reason}\n<!-- /neon-expired:v1 -->"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _now_iso() -> str:
    return _now().isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
