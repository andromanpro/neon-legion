#!/usr/bin/env python
"""Attribute AI session cost to git diff volume within each session window."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
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
    all_commits, git_errors = _commits_in_range(
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
        # DeepSeek audit MED #9 on PR #87: distinguish sessions with commits
        # but zero line changes (merge-only, rename-only, chmod-only) from
        # true no_diff sessions. They share `no_diff=true` semantics for the
        # percentile pool but the structural difference is visible in the
        # JSON for any consumer that wants it.
        zero_line_diff = total_lines == 0
        payload_sessions.append(
            {
                "session_id": session_id,
                "session_short": session_id[:8],
                "start_ts": bucket["start"].isoformat(timespec="seconds"),
                "end_ts": bucket["end"].isoformat(timespec="seconds"),
                "cost_usd": bucket["cost"],
                "commits": commits,
                "total_lines_changed": total_lines,
                "cost_per_line_usd": None if zero_line_diff else bucket["cost"] / total_lines,
                "no_diff": zero_line_diff,
                "session_has_commits_but_zero_lines": zero_line_diff,
            }
        )

    with_commits = [
        item for item in payload_sessions
        if not item["no_diff"] and item.get("cost_per_line_usd") is not None
    ]
    threshold = _percentile([float(item["cost_per_line_usd"]) for item in with_commits], top_decile_threshold)
    expensive = []
    if threshold is not None:
        expensive = [item for item in with_commits if float(item["cost_per_line_usd"]) >= threshold]
        expensive.sort(key=lambda item: float(item["cost_per_line_usd"]), reverse=True)
        expensive = expensive[:5]

    base["sessions"] = payload_sessions
    base["expensive_sessions"] = expensive
    # DeepSeek audit MED #6 on PR #87: surface git_errors as a structural
    # JSON field. Operators who only consume diff_cost.json (no stderr access)
    # can see whether commit reads were silently dropped.
    base["git_errors"] = list(git_errors)
    base["summary"] = {
        "total_sessions_scanned": len(payload_sessions),
        "sessions_with_commits": len(with_commits),
        "no_diff_count": len(payload_sessions) - len(with_commits),
        "expensive_lines_threshold_usd_per_line": threshold,
        "expensive_sessions_count": len(expensive),
        "git_errors_count": len(git_errors),
    }
    return base


def build_multi_repo_diff_cost(
    events: list[dict],
    repos: list[tuple[str, Path]],
    *,
    lookback_days: int = 30,
    top_decile_threshold: float = 0.9,
    now: datetime | None = None,
) -> dict:
    """Return diff_cost.json payload aggregated across multiple repositories.

    Session cost is attributed once at the top level. Git diff lines are summed
    across all configured repositories, and per_repo is only a line/commit lens.
    """
    current = _aware(now or datetime.now().astimezone())
    normalized_repos = [(str(name), _path(path)) for name, path in repos]
    primary_repo = normalized_repos[0][1] if normalized_repos else PROJECT_ROOT
    lookback_days = max(1, int(lookback_days))
    top_decile_threshold = float(top_decile_threshold)

    base = {
        "schema_version": 1,
        "generated_at": current.isoformat(timespec="seconds"),
        "config": {
            "repo_path": str(primary_repo),
            "repos": [name for name, _repo in normalized_repos],
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
        "per_repo": {
            name: {
                "sessions_with_commits": 0,
                "total_lines": 0,
                "expensive_sessions_count": 0,
            }
            for name, _repo in normalized_repos
        },
    }

    sessions = _session_buckets(events, current, lookback_days)
    if not sessions:
        return base

    range_start = min(bucket["start"] for bucket in sessions.values())
    range_end = max(bucket["end"] for bucket in sessions.values())
    git_errors: list[str] = []
    all_commits: list[dict[str, Any]] = []
    repo_order = {name: index for index, (name, _repo) in enumerate(normalized_repos)}
    for repo_name, repo_path in normalized_repos:
        if not _is_git_repo(repo_path):
            msg = f"not a git repository or git unavailable: {repo_path}"
            _log(msg)
            git_errors.append(msg)
            continue
        commits, errors = _commits_in_range(repo_path, range_start, range_end)
        git_errors.extend(errors)
        for commit in commits:
            tagged = dict(commit)
            tagged["repo"] = repo_name
            tagged["_repo_order"] = repo_order[repo_name]
            all_commits.append(tagged)

    all_commits.sort(
        key=lambda commit: (
            commit["_committed_at"],
            -int(commit.get("_repo_order", 0)),
            commit.get("hash", ""),
        ),
        reverse=True,
    )

    # Codex-audit HIGH: the old loop attributed EVERY commit in ANY repo to
    # EVERY session overlapping it in time — 306 commits ballooned into 3,077
    # session-attributions (one commit credited to 15 sessions), so $/line and
    # the expensive top were fiction. A commit now gets exactly ONE owner:
    #   tier 1 — among time-overlapping sessions, those whose working_dir sits
    #            inside the commit's repo (specific cwd beats the generic
    #            multi-project root, which is a parent of every repo and
    #            discriminates nothing);
    #   tier 2 — otherwise all overlapping sessions;
    #   pick   — the session with an event closest in time to the commit (the
    #            session that produced a commit has events dense around it).
    # Cross-session mis-attribution is still possible for truly concurrent
    # work, but each line is counted once globally instead of N times.
    repo_paths = {name: repo_path for name, repo_path in normalized_repos}
    session_commits: dict[str, list[dict[str, Any]]] = {sid: [] for sid in sessions}
    for commit in all_commits:
        committed_at = commit["_committed_at"]
        candidates = [
            sid
            for sid, bucket in sessions.items()
            if bucket["start"] <= committed_at <= bucket["end"]
        ]
        if not candidates:
            continue
        repo_path = repo_paths.get(str(commit.get("repo")))
        tier1 = [
            sid
            for sid in candidates
            if repo_path is not None
            and _wd_inside_repo(sessions[sid].get("working_dir", ""), repo_path)
        ]
        pool = tier1 or candidates
        moment = committed_at.timestamp()
        owner = min(pool, key=lambda sid: _nearest_event_gap(sessions[sid]["ts"], moment))
        session_commits[owner].append(commit)

    payload_sessions = []
    repo_session_lines: dict[str, dict[str, int]] = {
        session_id: {name: 0 for name, _repo in normalized_repos}
        for session_id in sessions
    }
    for session_id, bucket in sorted(sessions.items(), key=lambda item: (item[1]["start"], item[0])):
        commits = [_public_commit(commit) for commit in session_commits[session_id]]
        if not commits:
            payload_sessions.append(_no_diff_session(session_id, bucket))
            continue

        total_lines = 0
        for commit in commits:
            lines = int(commit["insertions"]) + int(commit["deletions"])
            total_lines += lines
            repo_session_lines[session_id][str(commit["repo"])] += lines

        zero_line_diff = total_lines == 0
        payload_sessions.append(
            {
                "session_id": session_id,
                "session_short": session_id[:8],
                "start_ts": bucket["start"].isoformat(timespec="seconds"),
                "end_ts": bucket["end"].isoformat(timespec="seconds"),
                "cost_usd": bucket["cost"],
                "commits": commits,
                "total_lines_changed": total_lines,
                "cost_per_line_usd": None if zero_line_diff else bucket["cost"] / total_lines,
                "no_diff": zero_line_diff,
                "session_has_commits_but_zero_lines": zero_line_diff,
            }
        )

    with_commits = [
        item for item in payload_sessions
        if not item["no_diff"] and item.get("cost_per_line_usd") is not None
    ]
    threshold = _percentile([float(item["cost_per_line_usd"]) for item in with_commits], top_decile_threshold)
    expensive = []
    if threshold is not None:
        expensive = [item for item in with_commits if float(item["cost_per_line_usd"]) >= threshold]
        expensive.sort(key=lambda item: float(item["cost_per_line_usd"]), reverse=True)
        expensive = expensive[:5]

    expensive_ids = {item["session_id"] for item in expensive}
    for repo_name, _repo_path in normalized_repos:
        repo_positive_sessions = {
            session_id
            for session_id, lines_by_repo in repo_session_lines.items()
            if lines_by_repo[repo_name] > 0
        }
        base["per_repo"][repo_name] = {
            "sessions_with_commits": len(repo_positive_sessions),
            "total_lines": sum(lines_by_repo[repo_name] for lines_by_repo in repo_session_lines.values()),
            "expensive_sessions_count": len(repo_positive_sessions & expensive_ids),
        }

    base["sessions"] = payload_sessions
    base["expensive_sessions"] = expensive
    base["git_errors"] = list(git_errors)
    base["summary"] = {
        "total_sessions_scanned": len(payload_sessions),
        "sessions_with_commits": len(with_commits),
        "no_diff_count": len(payload_sessions) - len(with_commits),
        "expensive_lines_threshold_usd_per_line": threshold,
        "expensive_sessions_count": len(expensive),
        "git_errors_count": len(git_errors),
    }
    return base


def public_payload(payload: dict) -> dict:
    """Trim the payload to exactly what the public dashboard consumes.

    The full payload leaks: `sessions` (every session's full UUID), `config`
    (absolute Windows repo paths), `per_repo` (private project names),
    `git_errors` (paths), and per-commit `hash`/`repo`/`files_changed`.
    The widget renders only the top expensive sessions — short id, dates,
    cost, line counts and commit subjects (subjects ARE displayed publicly
    by design: the start→final arc) — plus summary counters.
    """
    expensive = []
    for session in payload.get("expensive_sessions", []) or []:
        commits = [
            {
                "subject": str(commit.get("subject") or ""),
                "insertions": commit.get("insertions") or 0,
                "deletions": commit.get("deletions") or 0,
            }
            for commit in (session.get("commits") or [])
        ]
        expensive.append(
            {
                "session_short": session.get("session_short")
                or str(session.get("session_id") or "")[:8],
                "start_ts": session.get("start_ts"),
                "end_ts": session.get("end_ts"),
                "cost_usd": session.get("cost_usd"),
                "total_lines_changed": session.get("total_lines_changed"),
                "cost_per_line_usd": session.get("cost_per_line_usd"),
                "commits": commits,
            }
        )
    return {
        "schema_version": payload.get("schema_version"),
        "generated_at": payload.get("generated_at"),
        "public": True,
        "summary": payload.get("summary", {}),
        "expensive_sessions": expensive,
    }


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
    output = _path(args.output or cfg.get("git_diff_cost.output_path", str(DEFAULT_OUTPUT), str))
    try:
        repos = _parse_repo_specs(args.repos if args.repos is not None else cfg.get("git_diff_cost.repos", []))
    except ValueError as exc:
        _log(str(exc))
        return 2

    now = datetime.now().astimezone()
    start = now.date() - timedelta(days=max(1, int(lookback_days)) - 1)
    events = summary.read_events(start, now.date())
    if repos:
        payload = build_multi_repo_diff_cost(events, repos, lookback_days=lookback_days, top_decile_threshold=top_decile, now=now)
    else:
        repo = _path(args.repo or cfg.get("git_diff_cost.repo_path", str(PROJECT_ROOT), str))
        payload = build_diff_cost(events, repo, lookback_days=lookback_days, top_decile_threshold=top_decile, now=now)
    if args.public:
        payload = public_payload(payload)
    write_diff_cost(payload, output)
    _log(f"wrote {output}{' (public)' if args.public else ''}")
    _log(
        "sessions={total} no_diff={no_diff} expensive={expensive}{repos}".format(
            total=payload["summary"]["total_sessions_scanned"],
            no_diff=payload["summary"]["no_diff_count"],
            expensive=payload["summary"]["expensive_sessions_count"],
            repos=f" repos={len(repos)}" if repos else "",
        )
    )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Attribute session cost to git diff volume.")
    parser.add_argument("--repo", type=Path, help="Git repository path to scan.")
    parser.add_argument("--output", type=Path, help="Path to write diff_cost.json.")
    parser.add_argument("--repos", help='Comma-separated multi-repo specs: "name:path,name:path".')
    parser.add_argument("--lookback-days", type=int, help="How many days of session events to scan.")
    parser.add_argument("--top-decile", type=float, help="Percentile threshold for expensive lines, default 0.9.")
    parser.add_argument(
        "--public",
        action="store_true",
        help=(
            "Emit only what the public dashboard consumes (summary + top expensive "
            "sessions, short ids, subjects). Drops full session UUIDs, repo paths, "
            "project names and commit hashes."
        ),
    )
    return parser.parse_args()


def _parse_repo_specs(value: Any) -> list[tuple[str, Path]]:
    if value is None:
        return []
    if isinstance(value, str):
        entries = [entry.strip() for entry in value.split(",")]
    elif isinstance(value, (list, tuple)):
        entries = list(value)
    else:
        raise ValueError("git_diff_cost.repos must be a list of name:path strings")

    repos: list[tuple[str, Path]] = []
    seen_names: set[str] = set()
    seen_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, str):
            raise ValueError("git_diff_cost.repos entries must be strings")
        entry = entry.strip()
        if not entry:
            continue
        name, sep, path = entry.partition(":")
        name = name.strip()
        path = path.strip()
        if not sep or not name or not path:
            raise ValueError(f"invalid git_diff_cost.repos entry {entry!r}; expected name:path")
        if name in seen_names:
            raise ValueError(f"duplicate git_diff_cost.repos name {name!r}")
        # DeepSeek LOW #1 (money-math guard): two names pointing at the same
        # path would query the same commits twice → that session's lines
        # double-counted. Dedupe on resolved path, not just name.
        resolved = str(Path(path).resolve())
        if resolved in seen_paths:
            raise ValueError(f"duplicate git_diff_cost.repos path {path!r} (already configured under another name)")
        seen_names.add(name)
        seen_paths.add(resolved)
        repos.append((name, Path(path)))
    return repos


def _session_buckets(events: list[dict], now: datetime, lookback_days: int) -> dict[str, dict[str, Any]]:
    cutoff = now - timedelta(days=lookback_days)
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"start": None, "end": None, "cost": 0.0, "ts": [], "working_dirs": Counter()}
    )
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
        bucket["ts"].append(ts.timestamp())
        working_dir = event.get("working_dir")
        if isinstance(working_dir, str) and working_dir:
            bucket["working_dirs"][working_dir] += 1
    result = {}
    for sid, bucket in buckets.items():
        if bucket["start"] is None or bucket["end"] is None:
            continue
        bucket["ts"].sort()
        bucket["working_dir"] = (
            bucket["working_dirs"].most_common(1)[0][0] if bucket["working_dirs"] else ""
        )
        result[sid] = bucket
    return result


def _norm_dir(path_text: str) -> str:
    """Normalize a directory string for containment checks: repair the classic
    cp1251-of-utf8 mojibake some hooks recorded for Cyrillic paths, unify
    slashes, strip trailing separators, casefold (Windows FS)."""
    text = str(path_text or "")
    try:
        repaired = text.encode("cp1251").decode("utf-8")
        # Only accept the repair if it actually removed mojibake markers.
        if "Р" in text or "С" in text:
            text = repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return text.replace("\\", "/").rstrip("/").casefold()


def _wd_inside_repo(working_dir: str, repo_path: Path) -> bool:
    wd = _norm_dir(working_dir)
    repo = _norm_dir(str(repo_path))
    if not wd or not repo:
        return False
    return wd == repo or wd.startswith(repo + "/")


def _nearest_event_gap(sorted_ts: list[float], moment: float) -> float:
    """Seconds from `moment` to the session's nearest event (bisect)."""
    if not sorted_ts:
        return float("inf")
    import bisect

    index = bisect.bisect_left(sorted_ts, moment)
    best = float("inf")
    if index < len(sorted_ts):
        best = min(best, abs(sorted_ts[index] - moment))
    if index > 0:
        best = min(best, abs(sorted_ts[index - 1] - moment))
    return best


def _commits_in_range(repo: Path, start: datetime, end: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (commits, errors). DeepSeek audit MED #6: errors propagated
    out so callers can surface them in the JSON instead of silently dropping
    commits with corrupted git objects."""
    errors: list[str] = []
    result = _git(
        repo,
        "log",
        f"--since={start.isoformat(timespec='seconds')}",
        f"--until={end.isoformat(timespec='seconds')}",
        "--format=%H%x1f%cI%x1f%s",
    )
    if result.returncode != 0:
        msg = f"git log failed for {repo}: {result.stderr.strip()}"
        _log(msg)
        errors.append(msg)
        return [], errors

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
        stats, err = _commit_stats(repo, commit_hash.strip(), subject.strip())
        if err is not None:
            errors.append(err)
        if stats is not None:
            stats["_committed_at"] = committed_at
            commits.append(stats)
    return commits, errors


def _commit_stats(repo: Path, commit_hash: str, subject: str) -> tuple[dict[str, Any] | None, str | None]:
    result = _git(repo, "show", "--stat", "--format=", commit_hash)
    if result.returncode != 0:
        msg = f"git show failed for {commit_hash[:12]}: {result.stderr.strip()}"
        _log(msg)
        return None, msg
    files_changed, insertions, deletions = _parse_shortstat(result.stdout)
    return {
        "hash": commit_hash[:7],
        "insertions": insertions,
        "deletions": deletions,
        "files_changed": files_changed,
        "subject": subject,
    }, None


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
    """Run git subprocess and decode stdout/stderr as UTF-8.

    Without explicit `encoding`, `text=True` defaults to the system locale
    encoding (cp1251 on Windows-RU), which mangles non-ASCII commit subjects
    into cp1251 mojibake (e.g. `демо с` → `РґРµРјРѕ СЃ`). git emits UTF-8
    by default for log/diff output, so force UTF-8 decode.
    """
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
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
