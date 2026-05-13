#!/usr/bin/env python
"""Attribute AI session cost to git diff volume within each session window."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tracker"))

from tools import config as cfg  # noqa: E402
import summary  # noqa: E402


DEFAULT_OUTPUT = PROJECT_ROOT / "tracker" / "diff_cost.json"
SHORTSTAT_RE = re.compile(r"(?:(\d+) files? changed)?(?:,\s*)?(?:(\d+) insertions?\(\+\))?(?:,\s*)?(?:(\d+) deletions?\(-\))?")


def build_diff_cost(
    events: list[dict],
    repo_path: Path,
    *,
    lookback_days: int = 30,
    top_decile_threshold: float = 0.9,
    now: datetime | None = None,
) -> dict:
    """Return diff_cost.json payload."""
    current = _aware(now or datetime.now().astimezone())
    repo = _path(repo_path)
    lookback_days = max(1, int(lookback_days))
    top_decile_threshold = float(top_decile_threshold)

    base = {
        "schema_version": 1,
        "generated_at": current.isoformat(timespec="seconds"),
        "config": {
            "repo_path": str(repo),
            "lookback_days": lookback_days,
            "top_decile_threshold": top_decile_threshold,
        },
        "sessions": [],
        "expensive_sessions": [],
        "summary": {
            "total_sessions_scanned": 0,
            "sessions_with_commits": 0,
            "no_diff_count": 0,
            "expensive_lines_threshold_usd_per_line": None,
            "expensive_sessions_count": 0,
        },
    }
    if not _is_git_repo(repo):
        _log(f"not a git repository or git unavailable: {repo}")
        return base

    sessions = _session_buckets(events, current, lookback_days)
    if not sessions:
        return base
    all_commits = _commits_in_range(
        repo,
        min(bucket["start"] for bucket in sessions.values()),
        max(bucket["end"] for bucket in sessions.values()),
    )
    payload_sessions = []
    for session_id, bucket in sorted(sessions.items(), key=lambda item: (item[1]["start"], item[0])):
        commits = [
            _public_commit(commit)
            for commit in all_commits
            if bucket["start"] <= commit["_committed_at"] <= bucket["end"]
        ]
        if not commits:
            payload_sessions.append(_no_diff_session(session_id, bucket))
            continue

        total_lines = sum(int(commit["insertions"]) + int(commit["deletions"]) for commit in commits)
        payload_sessions.append(
            {
                "session_id": session_id,
                "session_short": session_id[:8],
                "start_ts": bucket["start"].isoformat(timespec="seconds"),
                "end_ts": bucket["end"].isoformat(timespec="seconds"),
                "cost_usd": bucket["cost"],
                "commits": commits,
                "total_lines_changed": total_lines,
                "cost_per_line_usd": bucket["cost"] / max(total_lines, 1),
                "no_diff": False,
            }
        )

    with_commits = [item for item in payload_sessions if not item["no_diff"]]
    threshold = _percentile([float(item["cost_per_line_usd"]) for item in with_commits], top_decile_threshold)
    expensive = []
    if threshold is not None:
        expensive = [item for item in with_commits if float(item["cost_per_line_usd"]) >= threshold]
        expensive.sort(key=lambda item: float(item["cost_per_line_usd"]), reverse=True)
        expensive = expensive[:5]

    base["sessions"] = payload_sessions
    base["expensive_sessions"] = expensive
    base["summary"] = {
        "total_sessions_scanned": len(payload_sessions),
        "sessions_with_commits": len(with_commits),
        "no_diff_count": len(payload_sessions) - len(with_commits),
        "expensive_lines_threshold_usd_per_line": threshold,
        "expensive_sessions_count": len(expensive),
    }
    return base


def write_diff_cost(payload: dict, output_path: Path) -> None:
    """Atomic write."""
    path = _path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def main() -> int:
    """CLI."""
    args = _parse_args()
    lookback_days = args.lookback_days if args.lookback_days is not None else cfg.get("git_diff_cost.lookback_days", 30, int)
    top_decile = args.top_decile if args.top_decile is not None else cfg.get("git_diff_cost.top_decile_threshold", 0.9, float)
    repo = _path(args.repo or cfg.get("git_diff_cost.repo_path", str(PROJECT_ROOT), str))
    output = _path(args.output or cfg.get("git_diff_cost.output_path", str(DEFAULT_OUTPUT), str))

    now = datetime.now().astimezone()
    start = now.date() - timedelta(days=max(1, int(lookback_days)) - 1)
    events = summary.read_events(start, now.date())
    payload = build_diff_cost(events, repo, lookback_days=lookback_days, top_decile_threshold=top_decile, now=now)
    write_diff_cost(payload, output)
    _log(f"wrote {output}")
    _log(
        "sessions={total} no_diff={no_diff} expensive={expensive}".format(
            total=payload["summary"]["total_sessions_scanned"],
            no_diff=payload["summary"]["no_diff_count"],
            expensive=payload["summary"]["expensive_sessions_count"],
        )
    )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Attribute session cost to git diff volume.")
    parser.add_argument("--repo", type=Path, help="Git repository path to scan.")
    parser.add_argument("--output", type=Path, help="Path to write diff_cost.json.")
    parser.add_argument("--lookback-days", type=int, help="How many days of session events to scan.")
    parser.add_argument("--top-decile", type=float, help="Percentile threshold for expensive lines, default 0.9.")
    return parser.parse_args()


def _session_buckets(events: list[dict], now: datetime, lookback_days: int) -> dict[str, dict[str, Any]]:
    cutoff = now - timedelta(days=lookback_days)
    buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {"start": None, "end": None, "cost": 0.0})
    for event in events:
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        ts = summary.parse_event_ts(event.get("ts"))
        if ts is None:
            continue
        ts = _aware(ts, now.tzinfo)
        if ts < cutoff or ts > now:
            continue
        bucket = buckets[session_id]
        bucket["start"] = ts if bucket["start"] is None else min(bucket["start"], ts)
        bucket["end"] = ts if bucket["end"] is None else max(bucket["end"], ts)
        bucket["cost"] += summary.as_float(event.get("cost_estimate_usd"))
    return {sid: bucket for sid, bucket in buckets.items() if bucket["start"] is not None and bucket["end"] is not None}


def _commits_in_range(repo: Path, start: datetime, end: datetime) -> list[dict[str, Any]]:
    result = _git(
        repo,
        "log",
        f"--since={start.isoformat(timespec='seconds')}",
        f"--until={end.isoformat(timespec='seconds')}",
        "--format=%H%x1f%cI%x1f%s",
    )
    if result.returncode != 0:
        _log(f"git log failed for {repo}: {result.stderr.strip()}")
        return []

    commits = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f", 2)
        if len(parts) != 3:
            continue
        commit_hash, committed_at_raw, subject = parts
        committed_at = _parse_git_datetime(committed_at_raw)
        if committed_at is None:
            continue
        stats = _commit_stats(repo, commit_hash.strip(), subject.strip())
        if stats is not None:
            stats["_committed_at"] = committed_at
            commits.append(stats)
    return commits


def _commit_stats(repo: Path, commit_hash: str, subject: str) -> dict[str, Any] | None:
    result = _git(repo, "show", "--stat", "--format=", commit_hash)
    if result.returncode != 0:
        _log(f"git show failed for {commit_hash[:12]}: {result.stderr.strip()}")
        return None
    files_changed, insertions, deletions = _parse_shortstat(result.stdout)
    return {
        "hash": commit_hash[:7],
        "insertions": insertions,
        "deletions": deletions,
        "files_changed": files_changed,
        "subject": subject,
    }


def _parse_shortstat(output: str) -> tuple[int, int, int]:
    for line in reversed([line.strip() for line in output.splitlines() if line.strip()]):
        if "changed" not in line:
            continue
        match = SHORTSTAT_RE.search(line)
        if match:
            return tuple(int(value or 0) for value in match.groups())  # type: ignore[return-value]
    return 0, 0, 0


def _no_diff_session(session_id: str, bucket: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "session_short": session_id[:8],
        "start_ts": bucket["start"].isoformat(timespec="seconds"),
        "end_ts": bucket["end"].isoformat(timespec="seconds"),
        "cost_usd": bucket["cost"],
        "no_diff": True,
    }


def _public_commit(commit: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in commit.items() if not key.startswith("_")}


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    percentile = max(0.0, min(1.0, percentile))
    index = int((len(values) - 1) * percentile)
    if index < len(values) - 1 and ((len(values) - 1) * percentile) > index:
        index += 1
    return values[index]


def _is_git_repo(repo: Path) -> bool:
    inside = _git(repo, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return False
    top_level = _git(repo, "rev-parse", "--show-toplevel")
    if top_level.returncode != 0:
        return False
    try:
        return Path(top_level.stdout.strip()).resolve() == repo.resolve()
    except OSError:
        return False


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    except (OSError, FileNotFoundError) as exc:
        return subprocess.CompletedProcess(["git", "-C", str(repo), *args], 127, "", str(exc))


def _path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if os.name == "nt" and path.root and not path.drive:
        return Path(f"{Path.cwd().drive}{path}")
    return path


def _aware(value: datetime, tz: Any = None) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=tz or datetime.now().astimezone().tzinfo)
    return value.astimezone(tz) if tz else value.astimezone()


def _parse_git_datetime(value: str) -> datetime | None:
    try:
        return _aware(datetime.fromisoformat(value))
    except ValueError:
        return None


def _log(message: str) -> None:
    print(f"[git-diff-cost] {message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
